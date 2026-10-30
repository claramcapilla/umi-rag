"""Etapa PDF→JSON del pipeline: envuelve el motor de ingesta in-repo.

Traza el resultado en `SourceDocument`. El chunking/embeddings/indexado (Fase 3) va aparte.

**Un documento con secciones fallidas no se indexa.** El motor informa de cuáles no pudo generar
en el bloque `_extraction`, y la decisión es de aquí: se guarda el JSON —con el bloque dentro, para
que el fichero archivado diga por sí mismo qué le falta—, el documento pasa a `FAILED` con el
detalle en `error_msg`, y queda reintentable desde el dashboard. Indexarlo sería peor que no
tenerlo: una guía sin su posología no responde «no lo sé», responde que la guía no dice nada al
respecto. El rechazo se repite en `index.py`, porque `FAILED → INDEXING` es una transición
permitida y aquí solo se cubre el camino automático.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from document_processor.consumo import midiendo
from document_processor.extraction import ConversionError
from documents.models import DocType, FidelityReport, SourceDocument, Stage, Status

from .errors import error_summary
from .metadata import extract_metadata

from .index import (
    index_document_task,
    retirar_documento_task,
    borrar_de_qdrant_task,
)

if TYPE_CHECKING:
    from django.db.models.fields.files import FieldFile

    from document_processor.sectioning import Chapter

logger = logging.getLogger(__name__)


def process_document(doc_id: str, cola: str | None = None) -> None:
    """PDF → JSON clínico rico para un `SourceDocument`, con estados y metadatos.

    El orquestador se importa de forma perezosa para no exigir la JVM ni el cliente del LLM salvo
    al procesar; `ConversionError` sí se importa arriba, porque hace falta en la cláusula `except`
    y su módulo no arrastra ninguna de las dos. Al terminar encadena el indexado en la cola
    `qdrant` (en modo EAGER corre en línea) **salvo que `INGEST_INDEXING_ENABLED` esté apagado**,
    en cuyo caso el documento queda en `JSON_READY` con su JSON en disco y no se toca Qdrant —
    **excepto si ya estaba indexado** (`qdrant_ids`): regenerar su JSON dejaría los chunks viejos
    obsoletos, así que se reencola el indexado para mantener Qdrant en sync aunque el flag esté off.
    Si se parte, en cambio, sale de Qdrant y sus capítulos no se indexan con el flag off.

    Las dos etapas se registran por separado: un PDF que no llega a Markdown falla en `PARSE`, y
    solo lo que ocurre del LLM en adelante es `JSON`. La distinción es de trazabilidad —`JSON`
    significa que hay un JSON incompleto en disco— y no cambia el reintento, que cubre las dos.

    El informe de fidelidad se persiste antes de leer la parcialidad: un documento parcial
    también lleva informe, y es justo el que más lo necesita. En un `ConversionError` no hay
    informe porque no hubo Markdown que medir.

    El volcado de metadatos, el guardado y la lectura de la parcialidad van **dentro** del
    `try`. Fuera de él, cualquier fallo en esas tres dejaba el documento en `PARSING` —estado no
    terminal que `retry_failed` no recoge, porque filtra por `FAILED`— y, si ya se había escrito
    el fichero con `save=False`, sin ninguna fila que lo referenciara.

    Es idempotente ante entregas duplicadas de la cola: un documento `ya_procesado` se omite. El
    broker Redis reentrega los mensajes reservados-sin-confirmar pasado `visibility_timeout`, y con
    tasks de 30+ min esa ventana se supera de sobra, así que la copia reentregada caía sobre un
    documento ya convertido; reprocesarlo repetiría todo el LLM y, además, `transition_to(PARSING)`
    lo rechazaba desde `JSON_READY` haciendo fallar la task con ruido en el log. El reintento
    legítimo no pasa por aquí: llega en `FAILED` —y `FAILED → PARSING` sí está permitida—. Un
    documento colgado en `PARSING` (worker caído a media conversión) no se puede reprocesar desde
    ahí, porque `PARSING → PARSING` no es una transición: `manage.py recuperar_parsing` lo pasa
    antes a `FAILED`.
    """
    doc = SourceDocument.objects.get(pk=doc_id)

    if doc.ya_procesado:
        logger.info(
            "process_document: %s ya está en %s; se omite (entrega duplicada de la cola)",
            doc.pk,
            doc.status,
        )
        return

    doc.transition_to(Status.PARSING)

    # Progreso en vivo: se actualiza sección a sección con `.update()` (sin fila de histórico ni
    # pisar otros campos) y **también sobre el objeto en memoria**, para que el `save()` final de
    # la transición a JSON_READY no reescriba un avance rancio —el que tenía la fila al cargarla—
    # sobre lo que dejaron los `.update()`. La primera llamada limpia lo que quedara de una
    # ejecución anterior.
    def _progreso(hechas: int, total: int, nombre: str) -> None:
        doc.progress_done = hechas
        doc.progress_total = total
        doc.progress_label = nombre
        SourceDocument.objects.filter(pk=doc.pk).update(
            progress_done=hechas, progress_total=total, progress_label=nombre
        )

    _progreso(0, 0, "")

    try:
        resultado = _convertir_midiendo(doc, _progreso, cola)
        if resultado is None:
            return
        clinical_json, fidelity = resultado
        # El Markdown intermedio viaja como clave privada; se saca antes de archivar el JSON para
        # no meterlo en el fichero que se trocea, y se guarda aparte para mostrarlo en el detalle.
        md_text = clinical_json.pop("_markdown", "")
        with transaction.atomic():
            if not _sigue_en_curso(doc):
                return
            persist_clinical_json(doc, clinical_json, md_text, preserve_biblio=bool(doc.parent_id))
            if fidelity is not None:
                FidelityReport.from_engine(doc, fidelity)
            rechazo = _resumen_de_extraccion_parcial(
                doc, clinical_json.get("_extraction")
            ) or _resumen_de_contenido_invalido(doc, clinical_json.get("_content_check"))
            if rechazo is not None:
                doc.transition_to(Status.FAILED, error_stage=Stage.JSON, error_msg=rechazo)
                return
            doc.transition_to(Status.PARSED, save=False)
            doc.transition_to(Status.JSON_READY)
            capitulos_viejos = _quitar_capitulos(doc)
    except ConversionError as exc:
        logger.exception("process_document: fallo convirtiendo el PDF de %s", doc.pk)
        _fallar(doc, Stage.PARSE, error_summary(exc))
        return
    except Exception as exc:  # noqa: BLE001 - se registra el fallo en el documento
        logger.exception("process_document: fallo generando el JSON de %s", doc.pk)
        _fallar(doc, Stage.JSON, error_summary(exc))
        return

    if settings.INGEST_INDEXING_ENABLED or doc.qdrant_ids:
        from .index import index_document_task

        index_document_task.delay(str(doc.pk))
    _encolar_borrado_de_qdrant(capitulos_viejos)


def persist_clinical_json(
    doc: SourceDocument,
    clinical_json: dict[str, Any],
    md_text: str = "",
    *,
    preserve_biblio: bool = False,
) -> None:
    """Vuelca metadatos + JSON clínico (+ Markdown) sobre `doc`, sin tocar el estado.

    Es lo que hace `process_document` tras el LLM, extraído para que también lo use la importación
    del corpus externo, que llega al mismo `JSON_READY` sin pasar por el modelo. Los ficheros se
    escriben con `save=False`: quien llama persiste la fila con la transición de estado posterior.

    `preserve_biblio` deja intactos los metadatos bibliográficos ya presentes: es lo que necesita un
    capítulo de una guía partida, que se siembra con el título/autores/año de la guía madre para que
    su cita nombre la fuente real y no el encabezado del capítulo. Sin esto, el metadato del propio
    capítulo pisaría esa siembra. Por lo mismo, el título del JSON del capítulo se sustituye por el
    de la guía madre.
    """
    if preserve_biblio:
        _titulo_de_la_fuente(clinical_json, doc.title)
    else:
        _aplicar_metadatos(doc, extract_metadata(clinical_json))
    _guardar_json(doc, clinical_json)
    _guardar_md(doc, md_text)


def _titulo_de_la_fuente(clinical_json: dict[str, Any], titulo: str) -> None:
    metadatos = clinical_json.get("metadata")
    if titulo and isinstance(metadatos, dict):
        metadatos["title"] = titulo


def _convertir_midiendo(
    doc: SourceDocument, on_progress: Callable[[int, int, str], None], cola: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    with midiendo() as consumo:
        try:
            return _convertir(doc, on_progress, cola)
        finally:
            doc.llm_usage = consumo.resumen()
            SourceDocument.objects.filter(pk=doc.pk, status=Status.PARSING).update(
                llm_usage=doc.llm_usage
            )


def _convertir(
    doc: SourceDocument, on_progress: Callable[[int, int, str], None], cola: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Produce el `(JSON clínico, informe de fidelidad)` del documento, o `None` si se dividió.

    Tres caminos, por lo que cada tipo necesita del Markdown:

    - **Capítulo** (`parent` no nulo): su Markdown ya está en disco, sin PDF; se genera directo
      con el esquema de guía, sin volver a enrutar.
    - **Enorme**: un Markdown por encima del límite de capítulo es un libro (guía o manual) que no
      cabe en una pieza con revisión, así que se divide en capítulos de como mucho ese límite, sea
      cual sea el tipo, y se devuelve `None`.
    - **Resto**: se genera el JSON del Markdown entero.

    Todo lo que falle antes del LLM sale como `ConversionError`; lo propaga el llamante.
    """
    from document_processor.orchestration import (
        md_to_clinical_json,
        pdf_bytes_to_markdown,
        pdf_bytes_to_tablas_en_rejilla,
    )
    from document_processor.orchestration.clinical_formatter import (
        CAPITULO_DE_GUIA,
        rechazar_ficha_tecnica,
    )
    from document_processor.sectioning import repartir_tablas, split_into_chapters

    if doc.parent_id:
        return md_to_clinical_json(
            leer_texto(doc.md_file),
            on_progress=on_progress,
            tablas_en_rejilla=leer_texto(doc.tables_file) if doc.tables_file else "",
            enrutado=CAPITULO_DE_GUIA,
        )

    with doc.pdf_file.open("rb") as fh:
        pdf_bytes = fh.read()

    md_text = pdf_bytes_to_markdown(pdf_bytes)
    limite = limite_de_capitulo()
    if len(md_text) > limite:
        rechazar_ficha_tecnica(md_text)
        capitulos = split_into_chapters(md_text, maximo=limite)
        tablas = repartir_tablas(
            pdf_bytes_to_tablas_en_rejilla(pdf_bytes), [capitulo.text for capitulo in capitulos]
        )
        _dividir_en_capitulos(doc, capitulos, tablas, cola)
        return None
    return md_to_clinical_json(
        md_text,
        on_progress=on_progress,
        tablas_en_rejilla=pdf_bytes_to_tablas_en_rejilla(pdf_bytes),
    )


def limite_de_capitulo() -> int:
    from document_processor import config
    from document_processor.llm_config import get_llm_whole_document

    if get_llm_whole_document():
        return min(settings.GUIDE_SPLIT_MAX_CHARS, config.CHUNK_MAX_CHARS)
    return settings.GUIDE_SPLIT_MAX_CHARS


def leer_texto(fichero: FieldFile) -> str:
    with fichero.open("rb") as fh:
        datos: bytes = fh.read()
    return datos.decode("utf-8")


def _dividir_en_capitulos(
    parent: SourceDocument, capitulos: list[Chapter], tablas: list[str], cola: str | None = None
) -> None:
    """Crea un documento hijo por capítulo, lo encola y marca la guía madre como `SPLIT`.

    Todo en una transacción para que un fallo a medias no deje capítulos sueltos con la madre aún en
    `PARSING`. Borra los capítulos previos antes de crear los nuevos, de modo que re-partir una guía
    (reproceso) no los duplique: sus `sha256` sintéticos son deterministas y colisionarían. Sus
    ficheros se borran a mano porque borrar la fila no borra el `FileField` de disco, y quedarían
    huérfanos en `MEDIA_ROOT`.

    El encolado va **después** del commit: `.delay()` dentro de la transacción puede entregar la
    tarea del capítulo a un worker antes de que su fila esté confirmada, y `process_document` la
    buscaría con `DoesNotExist`. Es el mismo orden que la subida (ingesta atómica, encolado fuera).
    """
    with transaction.atomic():
        if not _sigue_en_curso(parent):
            return
        objetivos = [_en_qdrant(parent), *_quitar_capitulos(parent)]
        _borrar_ficheros_al_confirmar([parent.json_file])
        parent.doc_type = DocType.GUIA_CLINICA
        hijos = [
            _crear_capitulo(parent, indice, capitulo, tablas_del_capitulo)
            for indice, (capitulo, tablas_del_capitulo) in enumerate(
                zip(capitulos, tablas, strict=True)
            )
        ]
        parent.json_file = None
        parent.qdrant_ids = []
        parent.chunk_count = 0
        parent.transition_to(Status.SPLIT)

    for hijo in hijos:
        process_document_task.apply_async(args=[str(hijo.pk)], queue=cola)
    _encolar_borrado_de_qdrant(objetivos)


def _quitar_capitulos(parent: SourceDocument) -> list[tuple[list[str], str]]:
    viejos = list(parent.chapters.all())
    _borrar_ficheros_al_confirmar(
        [fichero for doc in viejos for fichero in (doc.md_file, doc.json_file, doc.tables_file)]
    )
    parent.chapters.all().delete()
    return [_en_qdrant(doc) for doc in viejos]


def _borrar_ficheros_al_confirmar(ficheros: list[FieldFile]) -> None:
    rutas = [(fichero.storage, fichero.name) for fichero in ficheros if fichero.name]

    def borrar() -> None:
        for almacen, ruta in rutas:
            almacen.delete(ruta)

    transaction.on_commit(borrar)


def _encolar_borrado_de_qdrant(
    en_qdrant: list[tuple[list[str], str]],
) -> None:
    """Encola el borrado de puntos de Qdrant que han quedado obsoletos."""

    objetivos = [
        (ids, ruta)
        for ids, ruta in en_qdrant
        if ids
    ]

    if objetivos:
        from .index import borrar_de_qdrant_task

        borrar_de_qdrant_task.delay(objetivos)


def _fallar(doc: SourceDocument, etapa: str, mensaje: str) -> None:
    with transaction.atomic():
        if _sigue_en_curso(doc):
            doc.refresh_from_db()
            doc.transition_to(Status.FAILED, error_stage=etapa, error_msg=mensaje)


def _sigue_en_curso(doc: SourceDocument) -> bool:
    estado = (
        SourceDocument.objects.select_for_update()
        .filter(pk=doc.pk)
        .values_list("status", flat=True)
        .first()
    )
    if estado == Status.PARSING:
        return True
    logger.warning(
        "process_document: %s pasó a %s mientras convertía; no se guarda", doc.pk, estado
    )
    return False


def _en_qdrant(doc: SourceDocument) -> tuple[list[str], str]:
    return list(doc.qdrant_ids or []), doc.json_file.name or ""


def _crear_capitulo(
    parent: SourceDocument, indice: int, capitulo: Chapter, tablas: str
) -> SourceDocument:
    """Un capítulo es un `SourceDocument` sin PDF, con su porción de Markdown y la bibliografía
    de la guía madre sembrada para que la cita apunte a la fuente real.

    El `sha256` se sintetiza de forma determinista a partir del de la madre y el índice: un capítulo
    no tiene bytes de PDF propios, pero la columna es única y sostiene el nombre del fichero y el ID
    de los chunks, así que necesita un valor distinto por capítulo.
    """
    huella = hashlib.sha256(f"{parent.sha256}:{indice}".encode()).hexdigest()
    hijo = SourceDocument(
        sha256=huella,
        parent=parent,
        chapter_heading=capitulo.heading[:512],
        original_filename=parent.original_filename,
        doc_type=parent.doc_type,
        title=parent.title,
        authors=list(parent.authors or []),
        journal=parent.journal,
        year=parent.year,
        doi=parent.doi,
        pmid=parent.pmid,
    )
    hijo.md_file.save(f"{huella}.md", ContentFile(capitulo.text.encode("utf-8")), save=False)
    if tablas:
        hijo.tables_file.save(f"{huella}.md", ContentFile(tablas.encode("utf-8")), save=False)
    hijo.save()
    hijo.collections.set(parent.collections.all())
    return hijo


def _aplicar_metadatos(doc: SourceDocument, meta: dict[str, Any]) -> None:
    """Vuelca los metadatos extraídos sin borrar los que ya había.

    `extract_metadata` devuelve cadena vacía para lo que no encuentra. Asignarlo tal cual
    destruía el título y el DOI que el usuario escribió al subir el PDF —o que dejó
    `quick_metadata`— justo cuando la sección de metadatos era la que había fallado, y de forma
    permanente, porque ese documento ya no vuelve a avanzar.
    """
    doc.doc_type = meta["doc_type"] or doc.doc_type
    doc.title = _dentro_del_limite(doc, "title", meta["title"]) or doc.title
    doc.authors = meta["authors"] or doc.authors
    doc.journal = _dentro_del_limite(doc, "journal", meta["journal"]) or doc.journal
    doc.year = _dentro_del_limite(doc, "year", meta["year"]) or doc.year
    doc.doi = _dentro_del_limite(doc, "doi", meta["doi"]) or doc.doi
    doc.pmid = _dentro_del_limite(doc, "pmid", meta["pmid"]) or doc.pmid


def _dentro_del_limite(doc: SourceDocument, campo: str, valor: str) -> str:
    """Descarta un metadato que no cabe en su columna en vez de dejar que el `save()` reviente.

    El LLM puede devolver un `year` como rango o un `doi` como frase entera; la columna `year` son
    8 caracteres y `doi` 128, así que un valor más largo es casi siempre una alucinación, no un
    dato a truncar (truncarlo guardaría un identificador falso). Sin esta guarda, el valor llegaba
    al `save()` de la transición y lo tumbaba con `DataError`, fallando el documento en un punto
    que antes era seguro. `year` es el más expuesto por lo corto de su columna.
    """
    limite = getattr(doc._meta.get_field(campo), "max_length", None)
    return valor if limite is None or len(valor) <= limite else ""


def _guardar_json(doc: SourceDocument, clinical_json: dict[str, Any]) -> None:
    """Guarda el JSON clínico reemplazando el anterior, si lo hubiera.

    `FileSystemStorage` no sobrescribe: ante un nombre existente añade un sufijo aleatorio y deja
    el fichero viejo en disco sin ninguna fila que lo referencie. Antes de esta etapa un fallo en
    la generación no escribía nada, pero ahora el camino parcial sí escribe, así que cada
    reintento desde el dashboard iba dejando otra copia de JSON clínico huérfana en `MEDIA_ROOT`:
    invisible para la reconciliación, para el manifiesto de snapshots y para una petición de
    supresión.

    El payload se serializa **antes** de borrar. Un `FAILED` en etapa `INDEX` llega aquí con un
    JSON bueno en disco, y reprocesarlo con el LLM caído borraba ese JSON para no escribir nada a
    cambio. Queda una ventana de dos operaciones de almacenamiento consecutivas en la que la fila
    apunta a un fichero que ya no está; cerrarla del todo exige escribir con otro nombre y hacer
    el intercambio en la transacción, que es un cambio de modelo aparte.
    """
    payload = json.dumps(clinical_json, ensure_ascii=False, indent=2)

    if doc.json_file:
        doc.json_file.delete(save=False)

    doc.json_file.save(f"{doc.sha256}.json", ContentFile(payload.encode("utf-8")), save=False)


def _guardar_md(doc: SourceDocument, md_text: str) -> None:
    """Archiva el Markdown intermedio, reemplazando el anterior (mismo criterio que `_guardar_json`:
    `FileSystemStorage` no sobrescribe, así que se borra el viejo antes para no dejar huérfanos)."""
    if not md_text:
        return
    if doc.md_file:
        doc.md_file.delete(save=False)
    doc.md_file.save(f"{doc.sha256}.md", ContentFile(md_text.encode("utf-8")), save=False)


def _resumen_de_extraccion_parcial(doc: SourceDocument, extraccion: object) -> str | None:
    """Mensaje persistente de la parcialidad, o `None` si el motor no señaló ninguna.

    Los nombres de sección y los tipos de excepción son estáticos —salen de
    `SECTIONS_BY_VARIANT` y de `type(exc).__name__`—, así que se persisten en vez de dejarlos
    solo en el log, cuya retención y control de acceso los fija el despliegue. Lo que los hace
    estáticos es que el motor sea el único que escribe `_extraction`, no la forma del bloque.

    La forma se valida igualmente: el bloque viaja dentro del JSON clínico, que es salida de un
    LLM, y de esta función depende que un documento incompleto no se indexe. Una forma inesperada
    se trata como parcialidad —el lado conservador— en vez de propagar un `TypeError`.
    """
    if extraccion is None:
        return None

    if not isinstance(extraccion, dict):
        logger.error("process_document: bloque _extraction con forma inesperada en %s", doc.pk)
        return "Extracción parcial, secciones sin generar: ?"

    crudas = extraccion.get("failed_sections")
    errores = extraccion.get("errors")
    fallidas = [str(s) for s in crudas] if isinstance(crudas, list) else []
    motivos = errores if isinstance(errores, dict) else {}

    logger.error(
        "process_document: extracción parcial de %s (%s)", doc.pk, ", ".join(fallidas) or "?"
    )
    detalle = ", ".join(f"{s} ({motivos.get(s, 'desconocido')})" for s in fallidas) or "?"
    return f"Extracción parcial, secciones sin generar: {detalle}"


def _resumen_de_contenido_invalido(doc: SourceDocument, revision: object) -> str | None:
    if revision is None:
        return None
    crudas = revision.get("violations") if isinstance(revision, dict) else None
    violaciones = [str(v) for v in crudas] if isinstance(crudas, list) else []
    logger.error("process_document: contenido inválido en %s (%d)", doc.pk, len(violaciones))
    return f"Contenido inválido: {'; '.join(violaciones) or '?'}"


@shared_task(bind=True, name="pipeline.process_document")  # type: ignore[untyped-decorator]
def process_document_task(self: Any, doc_id: str) -> None:
    process_document(doc_id, (self.request.delivery_info or {}).get("routing_key"))


@shared_task(name="pipeline.ingest_ficha_from_cima")  # type: ignore[untyped-decorator]
def ingest_ficha_task(nregistro: str, especialidad_id: str) -> None:
    """Ingesta de una ficha desde CIMA, distinguiendo el fallo definitivo del transitorio.

    **Vive en este módulo por obligación, no por gusto**: `app.autodiscover_tasks()` importa solo
    `<app>.tasks`, así que una tarea declarada en `cima_ingest.py` —que nadie importa al arrancar—
    no se registra en el worker. Cuando estaba allí, el worker la descartaba como tarea desconocida
    mientras la página decía «N fichas encoladas», y ningún test lo veía porque en modo EAGER
    `.delay` corre en el mismo proceso que acaba de importar el módulo.

    Los fallos definitivos —no hay ficha segmentada, el contenido no cuadra con el gemelo, el
    registro está retirado, la especialidad no existe— se registran y la tarea termina, en vez de
    propagar y hacer que el worker los cuente como error reintentable. La indisponibilidad de CIMA
    sí propaga: es transitoria y hay que volver a intentarla.

    El import es diferido porque `cima_ingest` importa de aquí los dos ayudantes de guardado.
    """
    from .cima_ingest import FALLOS_DEFINITIVOS, ingest_ficha

    try:
        ingest_ficha(nregistro, especialidad_id)
    except FALLOS_DEFINITIVOS as exc:
        logger.warning("CIMA %s no se ingiere: %s", nregistro, error_summary(exc))


@shared_task(name="pipeline.sync_cima")  # type: ignore[untyped-decorator]
def sync_cima_task() -> None:
    from .cima_sync import sincronizar

    sincronizar()
