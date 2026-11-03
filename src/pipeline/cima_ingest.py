"""Ingesta de una ficha técnica desde CIMA: documento nuevo o alias de uno existente.

Alcance de este módulo: el ciclo de vida de un registro. Primera ingesta, reejecución idempotente,
cambio de contenido (el registro pasa al documento que coincide con su ficha actual), retirada y
vuelta. Un documento que se queda sin registros vigentes sale de Chroma y se conserva retirado.

La desduplicación se apoya en la identidad exacta de lo que se ingiere —el conjunto de hashes por
sección— y no en el `sha256` del PDF, que no sirve: los PDF de Montelukast Cinfa 4 mg y 5 mg pesan
los mismos 226.693 bytes y tienen hashes distintos, mientras sus 21 secciones son idénticas byte a
byte.

Al añadir o soltar un alias **no se reindexa**, y no es un olvido: lo que cambia son
`national_codes` y `reimbursement`, y ninguno de los dos está entre los 25 campos que declara
`SECTIONS_FT`, así que el troceado no los ve. El texto es idéntico por definición. Las excepciones
son una especialidad que el documento no tenía (los chunks se generan por especialidad), un cambio
del registro que presenta el documento (cambia la cita de cada chunk) y un documento retirado que
vuelve.
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from document_processor.cima import build_ficha_from_cima
from document_processor.cima.atc import main_groups, specialties_for
from document_processor.cima.errors import CimaSinFicha
from documents.models import (
    CimaRegistration,
    DocType,
    FidelityReport,
    SourceDocument,
    Status,
    TipoEvento,
)
from library.models import Collection

from .errors import ColapsoDudoso, RegistroCambiado, RegistroRetirado, SinEspecialidad
from .metadata import extract_metadata
from .tasks import _aplicar_metadatos, _guardar_json, retirar_documento_task

logger = logging.getLogger(__name__)


def _huella(clinical_json: dict[str, Any]) -> str:
    """Identidad de lo que se ingiere: el conjunto de hashes por sección, en orden estable.

    Deja fuera a propósito los metadatos estructurados —concentración, códigos nacionales,
    nombre—, que son justo lo que distingue a dos registros del mismo SmPC y lo que el alias
    conserva. Y deja fuera lo que no se ingiere, como la sección 5.3, porque la identidad que
    importa es la del contenido archivado.
    """
    hashes = clinical_json["_source"]["section_hashes"]
    canonico = json.dumps(hashes, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonico.encode()).hexdigest()


def _descriptor_de_alias(nregistro: str, clinical_json: dict[str, Any]) -> dict[str, Any]:
    """Lo que distingue a un registro colapsado, que es lo que no se puede perder al unirlos.

    Dentro de un grupo del censo variaba la concentración en 37 de 44 casos, el nombre en 42 y
    **la comercialización en 2**: sin registrar esto último se serviría como vigente una
    presentación retirada.
    """
    fuente = clinical_json["_source"]
    return {
        "registration_number": nregistro,
        "name": fuente["name"],
        "strength": clinical_json["strength"],
        "pharmaceutical_form": clinical_json["pharmaceutical_form"],
        "national_codes": clinical_json["national_codes"],
        "pdf_url": fuente["pdf_url"],
        "smpc_date": fuente["smpc_date"],
    }


def _sin_repetir(previos: list[Any], nuevos: list[Any]) -> list[Any]:
    return previos + [n for n in nuevos if n not in previos]


def ingest_ficha(nregistro: str, especialidad_id: str) -> SourceDocument:
    """Ingiere la ficha técnica de un número de registro y devuelve su documento.

    Cuatro desenlaces: documento nuevo, alias de uno ya ingerido, nada que hacer porque este
    registro ya está y su contenido no ha cambiado, o el registro pasa al documento que coincide
    con su contenido actual porque su ficha cambió en CIMA.

    La especialidad es **obligatoria** y la elige quien ingiere, igual que en la subida. Sin ella
    el documento no está en ninguna colección raíz y `index_document` lo rechaza, así que la ficha
    llegaría hasta `JSON_READY` para morir en `INDEX`: un fallo al final de todo el trabajo, con
    las peticiones al registro ya gastadas.

    **La carrera la resuelve la base de datos, no el orden de la cola.** El worker corre con cuatro
    hilos y cada ficha hace sus peticiones al registro antes de la comprobación del gemelo, así que
    varios alias del mismo SmPC leen «no hay gemelo» a la vez. Quien pierde la unicidad de la huella
    (`SourceDocument.sha256`, y detrás el índice parcial de `CimaRegistration`) recibe
    `IntegrityError`, la transacción deshace su documento a medias y vuelve por el camino del alias.
    Si el ganador ya tiene este número de registro —un doble envío—, devuelve su documento. Suponer
    que la cola serializaba era un error: es una cola, no un hilo.

    **BIFIMED se consulta antes de abrir la transacción**, y solo en la rama que crea documento.
    Dentro, la consulta —con sus reintentos, su descarga de varios MB y su plazo de pared— dejaba
    abierta una transacción de Postgres con la fila de `CimaRegistration` ya insertada y sus
    bloqueos del índice único tomados, durante minutos. Un alias no la paga porque no la necesita:
    hereda el bloque del documento canónico. El precio es consultar en vano cuando se pierde la
    carrera, que es raro y barato comparado con el otro.
    """
    especialidad = Collection.destino_de_ficha(especialidad_id)
    if especialidad is None:
        raise SinEspecialidad(
            f"La especialidad {especialidad_id!r} no existe o no es una colección raíz"
        )

    registro = CimaRegistration.objects.filter(pk=nregistro).first()
    if registro is not None and not registro.activo:
        raise RegistroRetirado(
            f"{nregistro} está retirado de CIMA ({registro.motivo_retirada}); si vuelve, lo "
            "reactiva la sincronización"
        )

    clinical_json, informe = build_ficha_from_cima(nregistro)
    return _ingerir(nregistro, clinical_json, informe, [especialidad])


def resincronizar(nregistro: str) -> tuple[SourceDocument, str | None]:
    clinical_json, informe = build_ficha_from_cima(nregistro)
    registro = CimaRegistration.objects.get(pk=nregistro)
    if registro.content_hash != _huella(clinical_json):
        tipo = TipoEvento.ACTUALIZADA if registro.activo else TipoEvento.REACTIVADA
        return _ingerir(nregistro, clinical_json, informe, []), tipo
    if not registro.activo:
        return _refrescar(registro, clinical_json), TipoEvento.REACTIVADA
    if registro.national_codes != clinical_json["national_codes"]:
        return _refrescar(registro, clinical_json), None
    return registro.document, None


def retirar_registro(nregistro: str, motivo: str) -> SourceDocument:
    with transaction.atomic():
        documento, registro = _bloquear(CimaRegistration.objects.get(pk=nregistro))
        registro.retirado_en = timezone.now()
        registro.motivo_retirada = motivo
        registro.save(update_fields=["retirado_en", "motivo_retirada"])
        _tras_cambiar_registros(documento)
    logger.info("CIMA %s retirado (%s) del documento %s", nregistro, motivo, documento.pk)
    return documento


def _ingerir(
    nregistro: str,
    clinical_json: dict[str, Any],
    informe: dict[str, Any],
    especialidades: list[Collection],
) -> SourceDocument:
    from document_processor.enrichment import enrich_ft_with_bifimed

    huella = _huella(clinical_json)
    registro = CimaRegistration.objects.filter(pk=nregistro).select_related("document").first()
    if registro is not None:
        if registro.content_hash == huella:
            logger.info("CIMA %s ya estaba ingerido y no ha cambiado", nregistro)
            return registro.document
        logger.info(
            "La ficha de CIMA %s cambió: deja el documento %s por el de su contenido actual",
            nregistro,
            registro.document_id,
        )
        especialidades = _sin_repetir(especialidades, list(registro.document.collections.all()))

    destino = SourceDocument.objects.filter(sha256=huella).first()
    if destino is None:
        clinical_json = enrich_ft_with_bifimed(clinical_json)
        try:
            with transaction.atomic():
                return _crear_documento(
                    nregistro, clinical_json, informe, huella, especialidades, registro
                )
        except IntegrityError:
            logger.info(
                "CIMA %s perdió la carrera por el documento canónico de su contenido; se cuelga "
                "como alias",
                nregistro,
            )
            destino = SourceDocument.objects.get(sha256=huella)

    with transaction.atomic():
        documento = SourceDocument.objects.select_for_update().get(pk=destino.pk)
        if documento.cima_registrations.filter(pk=nregistro).exists():
            return documento
        archivado = _cargar_archivado(documento)
        _comprobar_que_es_el_mismo_medicamento(archivado, documento, nregistro, clinical_json)
        _soltar(registro)
        return _anadir_alias(
            documento, archivado, nregistro, clinical_json, informe, huella, especialidades
        )


def _soltar(registro: CimaRegistration | None) -> None:
    if registro is None:
        return
    anterior, vigente = _bloquear(registro)
    vigente.delete()
    _tras_cambiar_registros(anterior)


def _bloquear(registro: CimaRegistration) -> tuple[SourceDocument, CimaRegistration]:
    documento = SourceDocument.objects.select_for_update().get(pk=registro.document_id)
    vigente = CimaRegistration.objects.select_for_update().get(pk=registro.pk)
    leido = (registro.document_id, registro.content_hash, registro.retirado_en)
    if (vigente.document_id, vigente.content_hash, vigente.retirado_en) != leido:
        raise RegistroCambiado(f"CIMA {registro.pk} cambió mientras se procesaba: se reintenta")
    return documento, vigente


def _refrescar(registro: CimaRegistration, clinical_json: dict[str, Any]) -> SourceDocument:
    with transaction.atomic():
        documento, registro = _bloquear(registro)
        registro.retirado_en = None
        registro.motivo_retirada = ""
        registro.national_codes = clinical_json["national_codes"]
        registro.save(update_fields=["retirado_en", "motivo_retirada", "national_codes"])
        archivado = _cargar_archivado(documento)
        if not registro.es_canonico:
            _poner_alias(archivado, registro.nregistro, clinical_json)
        _tras_cambiar_registros(documento, archivado)
    logger.info("CIMA %s vigente y al día en el documento %s", registro.pk, documento.pk)
    return documento


def _tras_cambiar_registros(
    documento: SourceDocument,
    archivado: dict[str, Any] | None = None,
    *,
    reindexar: bool = False,
) -> None:
    activos = list(
        documento.cima_registrations.filter(retirado_en__isnull=True).order_by(
            "-es_canonico", "created_at", "nregistro"
        )
    )
    if not activos:
        doc_id = str(documento.pk)
        transaction.on_commit(lambda: retirar_documento_task.delay(doc_id))
        return

    if archivado is None:
        archivado = _cargar_archivado(documento)
    reindexar = _recomponer(documento, archivado, activos) or reindexar
    if documento.status == Status.WITHDRAWN:
        documento.transition_to(Status.JSON_READY)
        reindexar = True
    if reindexar:
        transaction.on_commit(lambda: _encolar_indexado(documento))


def _recomponer(
    documento: SourceDocument, archivado: dict[str, Any], activos: list[CimaRegistration]
) -> bool:
    fuente = archivado["_source"]
    vigentes = {r.nregistro for r in activos}
    aliases = [a for a in fuente.get("aliases", []) if a["registration_number"] in vigentes]
    presentado = activos[0]
    cambia_de_presentacion = not presentado.es_canonico
    if cambia_de_presentacion:
        descriptor = next(
            (a for a in aliases if a["registration_number"] == presentado.nregistro), None
        )
        if descriptor is None:
            raise ColapsoDudoso(
                f"El documento {documento.pk} no guarda el descriptor de {presentado.nregistro}: "
                "no se puede presentar con él"
            )
        aliases.remove(descriptor)
        _presentar_como(archivado, descriptor)
        documento.cima_registrations.filter(es_canonico=True).update(es_canonico=False)
        presentado.es_canonico = True
        presentado.save(update_fields=["es_canonico"])
    fuente["aliases"] = aliases

    codigos: list[Any] = []
    for registro in activos:
        codigos = _sin_repetir(codigos, registro.national_codes)
    archivado["national_codes"] = codigos
    fuente["national_codes"] = codigos

    _guardar_json(documento, archivado)
    campos = ["json_file", "updated_at"]
    if cambia_de_presentacion:
        metadatos = extract_metadata(archivado)
        _aplicar_metadatos(documento, metadatos)
        documento.original_filename = f"FT_{presentado.nregistro}"
        campos += [*metadatos, "original_filename"]
    documento.save(update_fields=campos)
    return cambia_de_presentacion


def _presentar_como(archivado: dict[str, Any], descriptor: dict[str, Any]) -> None:
    fuente = archivado["_source"]
    if archivado.get("marketing_authorisation_number") == fuente["registration_number"]:
        archivado["marketing_authorisation_number"] = descriptor["registration_number"]
    for clave in ("registration_number", "name", "pdf_url", "smpc_date"):
        fuente[clave] = descriptor[clave]
    archivado["strength"] = descriptor["strength"]
    archivado["pharmaceutical_form"] = descriptor["pharmaceutical_form"]


def _poner_alias(archivado: dict[str, Any], nregistro: str, clinical_json: dict[str, Any]) -> None:
    fuente = archivado["_source"]
    fuente["aliases"] = [
        a for a in fuente.get("aliases", []) if a["registration_number"] != nregistro
    ] + [_descriptor_de_alias(nregistro, clinical_json)]


def _cargar_archivado(documento: SourceDocument) -> dict[str, Any]:
    with documento.json_file.open("rb") as fh:
        archivado: dict[str, Any] = json.loads(fh.read().decode("utf-8"))
    return archivado


def _comprobar_que_es_el_mismo_medicamento(
    archivado: dict[str, Any],
    documento: SourceDocument,
    nregistro: str,
    clinical_json: dict[str, Any],
) -> None:
    """Defensa en profundidad antes de unir dos registros bajo un documento.

    La huella cubre el contenido de las secciones normativas, y cuando CIMA segmenta las de
    identificación —1, 2, 7 y 8— eso ya distingue medicamento y titular. El hueco es la ficha a la
    que no se las segmentó: esos campos los rellena entonces el metadato estructurado y no entran
    en la huella, así que dos genéricos del mismo principio activo de laboratorios distintos, cuyas
    secciones 4.x a 6.x suelen ser idénticas, podrían igualar huella. No ha ocurrido en 195 grupos
    de un censo de 1.500 fichas; lo que justifica la guarda es que el fallo sería silencioso.
    """
    for campo in ("active_ingredient", "marketing_authorisation_holder"):
        if (archivado.get(campo) or "") != (clinical_json.get(campo) or ""):
            raise ColapsoDudoso(
                f"{nregistro} tiene el mismo contenido que el documento {documento.pk} pero "
                f"distinto {campo}; no se unen"
            )


def _anadir_alias(
    documento: SourceDocument,
    archivado: dict[str, Any],
    nregistro: str,
    clinical_json: dict[str, Any],
    informe: dict[str, Any],
    huella: str,
    especialidades: list[Collection],
) -> SourceDocument:
    """Cuelga otro número de registro del documento que ya contiene ese mismo SmPC.

    El texto no se toca —es el mismo, por definición de la huella— y lo que se añade es lo que
    distingue al registro colapsado más sus códigos nacionales, que es lo que permite que la
    financiación de BiFiMed cubra también sus presentaciones. Sin esto, colapsar sí perdería dato.

    **El bloque `reimbursement` no se recalcula.** Volver a enriquecer aquí destruía dato real:
    todas las ramas de `enrich_ft_with_bifimed` asignan el bloque, así que un alias añadido con el
    Ministerio caído lo sustituía por `unavailable`, y con `BIFIMED_ENABLED` apagado —el default—
    degradaba a `not_queried` **siempre**. Escribir «no pude preguntar» encima de «pregunté y esto
    hay» es exactamente la inversión que `BifimedUnavailable` existe para hacer imposible. Los
    códigos nacionales nuevos quedan en el JSON esperando el ciclo de refresco de la financiación,
    que la tarea 7 ya prevé aparte.

    **Las especialidades elegidas no se descartan.** Quien ingirió este registro pidió que fuera a
    una especialidad, y si la ficha resulta ser alias de otra que ya está en otra, ignorarlo en
    silencio sería tirar su decisión. Se añade —un documento puede estar en varias, y el indexado
    lo prevé— y **solo entonces se reindexa**, porque los chunks se generan por especialidad
    (`chunk_id(specialty, …)`): declarar una especialidad nueva sin reindexar dejaría un documento
    que dice estar en ella sin un solo chunk debajo. Si ya estaba, no se reindexa por eso: el texto
    es idéntico y `national_codes` y `reimbursement` no los ve el troceado.
    """
    _poner_alias(archivado, nregistro, clinical_json)
    CimaRegistration.objects.create(
        nregistro=nregistro,
        document=documento,
        content_hash=huella,
        national_codes=clinical_json["national_codes"],
    )
    FidelityReport.from_engine(documento, informe)

    nuevas = [e for e in especialidades if not documento.collections.filter(pk=e.pk).exists()]
    if nuevas:
        documento.collections.add(*nuevas)
        logger.info(
            "El documento %s pasa a estar también en %s: se reindexa",
            documento.pk,
            ", ".join(e.name for e in nuevas),
        )
    _tras_cambiar_registros(documento, archivado, reindexar=bool(nuevas))
    logger.info("CIMA %s es el mismo documento que %s: alias añadido", nregistro, documento.pk)
    return documento


def _encaminar_al_indexado(documento: SourceDocument) -> None:
    """La construcción desde CIMA **es** la etapa de parseo de este camino, así que se atraviesa
    `PARSING` en vez de saltarla: es lo que deja el historial de auditoría contando la verdad.
    """
    for estado in (Status.PARSING, Status.PARSED, Status.JSON_READY):
        documento.transition_to(estado)


def _encolar_indexado(documento: SourceDocument) -> None:
    """Encola el indexado de una ficha, si procede.

    Tres guardas, en orden. **El flag `INGEST_INDEXING_ENABLED`**, igual que en `process_document`:
    la ingesta de CIMA lo ignoraba y encolaba siempre, así que un despliegue en modo solo-JSON
    (el de inferencia local) indexaba fichas a espaldas de su propia política. La excepción es un
    documento ya indexado (`qdrant_ids`): regenerar su JSON dejaría sus chunks obsoletos, así que se
    reindexa aunque el flag esté apagado.

    **Una especialidad resoluble.** Las fichas del bucket no tienen ninguna (viven en su ventana, no
    en una especialidad), y `index_document` rechaza el documento sin especialidad **fuera de su
    `try`**: encolarlo sería un `FAILED` masivo garantizado. Se comprueba en el código y no solo por
    el flag, para que aguante también un despliegue con el indexado encendido. Un documento sin
    especialidad nunca se indexó, así que no puede tener `qdrant_ids` y la guarda del flag no lo
    salva por ese lado.

    **La transición a `INDEXING` posible**, que `index_document` hace fuera de su `try`: encolar
    sobre un documento que ya se indexa deja esa excepción sin capturar y sin marca de fallo.
    """
    if not (settings.INGEST_INDEXING_ENABLED or documento.qdrant_ids):
        return

    from .index import index_document_task, resolve_specialties

    if not resolve_specialties(documento):
        logger.info(
            "El documento %s no está en ninguna especialidad: no se indexa (ventana de fichas)",
            documento.pk,
        )
        return

    if not documento.can_transition_to(Status.INDEXING):
        logger.warning(
            "El documento %s está en %s: no se encola el indexado", documento.pk, documento.status
        )
        return

    index_document_task.delay(str(documento.pk))


def _crear_documento(
    nregistro: str,
    clinical_json: dict[str, Any],
    informe: dict[str, Any],
    huella: str,
    especialidades: list[Collection],
    anterior: CimaRegistration | None,
) -> SourceDocument:
    """**El registro canónico se crea en cuanto el documento tiene identidad**, antes de enriquecer
    con BiFiMed y de escribir el JSON: es la unicidad de la huella la que resuelve la carrera entre
    hilos, y quien la pierda debe enterarse antes de gastar una descarga del Ministerio.
    """
    documento = SourceDocument.objects.create(
        sha256=huella, doc_type=DocType.FICHA_TECNICA, original_filename=f"FT_{nregistro}"
    )
    _soltar(anterior)

    CimaRegistration.objects.create(
        nregistro=nregistro,
        document=documento,
        content_hash=huella,
        es_canonico=True,
        national_codes=clinical_json["national_codes"],
    )

    _aplicar_metadatos(documento, extract_metadata(clinical_json))
    documento.atc_groups = main_groups(clinical_json["_source"].get("atc"))
    documento.active_ingredient = str(clinical_json.get("active_ingredient") or "")[:512]
    _guardar_json(documento, clinical_json)
    documento.save()
    documento.collections.add(*especialidades, *_especialidades_por_atc(clinical_json))
    FidelityReport.from_engine(documento, informe)
    _encaminar_al_indexado(documento)
    transaction.on_commit(lambda: _encolar_indexado(documento))
    logger.info("CIMA %s ingerido como documento %s", nregistro, documento.pk)
    return documento


def _normalizar_especialidad(nombre: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", nombre.strip())
    return "".join(c for c in descompuesto if not unicodedata.combining(c)).casefold()


def _especialidades_por_atc(clinical_json: dict[str, Any]) -> list[Collection]:
    """Las especialidades raíz existentes a las que apunta el ATC de la ficha.

    El puente ATC→especialidad (`atc.specialties_for`) da nombres; aquí se resuelven contra las
    raíces `is_specialty_root` que existen de verdad, **sin crear ninguna**. Así una ficha del
    recorrido masivo, que entra al bucket, aparece además bajo su especialidad y —por tener ya una
    raíz resoluble— se vuelve indexable, mientras que un subgrupo cuyo nombre este despliegue no
    tenga deja la ficha solo en su ventana, que es la conducta previa al puente.

    El emparejamiento **ignora mayúsculas y tildes**: el nombre de la raíz lo teclea quien la crea
    (`clean_name` solo capitaliza la inicial), así que exigir la tilde exacta dejaría la ficha sin
    colgar en silencio. Si dos raíces normalizan al mismo nombre —posible, porque la unicidad de
    `name` no aplica con `parent` nulo— se cuelga de las dos: perder una sería nondeterminista.

    Solo actúa al crear el documento canónico. Una ficha ingerida **antes** de que existiera su raíz
    no se recuelga sola al crearla después —reingerir sin cambios vuelve temprano—: eso es trabajo
    del backfill/resincronización, que se decide aparte.
    """
    objetivo = {
        _normalizar_especialidad(n) for n in specialties_for(clinical_json["_source"].get("atc"))
    }
    if not objetivo:
        return []
    raices = Collection.objects.filter(parent__isnull=True, is_specialty_root=True)
    return [c for c in raices if _normalizar_especialidad(c.name) in objetivo]


FALLOS_DEFINITIVOS = (CimaSinFicha, ColapsoDudoso, RegistroRetirado, SinEspecialidad)
"""Lo que no mejora reintentando, y por tanto no debe propagar como error del worker.

La tarea Celery que los captura vive en `tasks.py` y no aquí: `app.autodiscover_tasks()` importa
**solo `<app>.tasks`**, así que una tarea declarada en este módulo no se registra en el worker.
Estaba así y el efecto era que el worker descartaba la tarea como desconocida mientras la página
decía «N fichas encoladas»; los tests no lo veían porque en modo EAGER `.delay` corre en el mismo
proceso que acaba de importar el módulo.
"""
