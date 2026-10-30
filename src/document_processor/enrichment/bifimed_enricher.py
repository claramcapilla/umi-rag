"""Enriquecimiento BIFIMED para fichas técnicas.

Consulta BIFIMED por los componentes del principio activo y conserva **solo las presentaciones
cuyo código nacional pertenece a esta ficha**, que es lo que hace que la financiación sea la del
medicamento citado y no la de otro que comparta la molécula.

Todo el módulo gira alrededor de una distinción: el bloque `financiacion` que se escribe aquí va
a un JSON clínico que se archiva y se cita, así que **«no pude consultar» nunca puede escribirse
como «consulté y no hay datos»**. Lo segundo afirma que un medicamento no está financiado; si en
realidad la consulta falló, es una afirmación falsa indistinguible de la verdadera. De ahí los
estados separados y el que la consulta viva en `_consultar`, aparte de la decisión de qué estado
se escribe.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from django.conf import settings
from django.core.cache import cache

from .bifimed.errors import BifimedUnavailable

if TYPE_CHECKING:
    from .bifimed.models import MedicamentoFinanciacion

logger = logging.getLogger(__name__)


class Consulta(NamedTuple):
    presentaciones: dict[str, dict[str, Any]]
    aportaron: list[str]
    fallidos: list[str]


def _componentes_de(principio_activo: str) -> list[str]:
    """Parte un principio activo en las DCI base que BIFIMED sabe buscar.

    Las combinaciones llegan como «salbutamol + beclometasona», y esa cadena devuelve **cero**
    presentaciones: medido contra el servicio, igual que «indacaterol + mometasona». Cada
    componente por separado sí encuentra el medicamento combinado —`SALBUTAMOL` devuelve el CN de
    Ventoduo, y `INDACATEROL` el de Bemrist—, así que se pregunta por componentes. Son 66 de las
    500 fichas del censo, un 13,2%, las que sin esto quedarían como no financiadas teniéndolo.
    """
    return [t for p in principio_activo.replace("+", ",").split(",") if (t := p.strip())]


def _financiacion_de(componente: str) -> MedicamentoFinanciacion | None:
    """Financiación de un principio activo, memoizada mientras dure el lote.

    Cada consulta descarga **el Excel entero** del Ministerio, y un lote de 50 fichas del mismo
    principio activo pagaba 50 descargas idénticas de varios MB.

    Se usa la caché de Django, no un diccionario propio, y **el candado no cubre la descarga**.
    Cubrirla parecía la forma de que dos hilos no la duplicasen, pero el precio era que un solo
    Ministerio lento bloqueara a todo el que pidiera otra cosa: con los reintentos y topes del
    cliente, hasta ~858 s de candado por clave, y con `--pool=threads` un `acquire` bloqueado no
    se puede interrumpir. Aquí el peor caso de la carrera es una descarga duplicada; el de la otra
    forma, la cola entera parada. `get_or_set` deja además el tope de entradas (`MAX_ENTRIES`) en
    manos del framework, y `BIFIMED_CACHE_SECONDS=0` lo desactiva sin ninguna rama especial.

    La vida es corta a propósito (diez minutos por defecto): esto es dato de financiación pública
    que se archiva y se cita, así que un lote largo no debe servir una foto de hace horas.

    **Un fallo no se memoiza y un «no hay datos» sí**, y la asimetría es deliberada: la excepción
    se propaga sin tocar la caché, mientras que `None` se memoiza porque `_get_financing` declara
    que significa una sola cosa, «la consulta se completó y BIFIMED no tiene datos». Esa invariante
    no se sostiene sola: `_download_excel` valida los magic bytes, así que una página de error con
    200 sale como `BifimedUnavailable` y no como `None`. Si esa validación cayera, memoizar `None`
    extendería la afirmación falsa «no financiado» a todas las fichas de la molécula.

    La clave va hasheada porque un principio activo lleva espacios y acentos, que son ilegales en
    memcached y avisan ya con `CacheKeyWarning`; el nombre en claro está en el log de la consulta.
    """
    from .bifimed import BifimedClient

    def consultar() -> MedicamentoFinanciacion | None:
        logger.info("Consultando BIFIMED: %s...", componente)
        return BifimedClient().get_financing_by_principio_activo(componente)

    clave = f"bifimed:{hashlib.sha256(componente.encode()).hexdigest()}"
    return cast(
        "MedicamentoFinanciacion | None",
        cache.get_or_set(clave, consultar, settings.BIFIMED_CACHE_SECONDS),
    )


def _consultar(componentes: list[str], cns: list[str]) -> Consulta:
    """Consulta por principio activo y conserva solo las presentaciones de esta ficha.

    **El filtro por código nacional es lo que sostiene la atribución.** El Excel de un principio
    activo trae todas sus presentaciones —«EPINEFRINA» devuelve 89, de las que 87 son anestésicos
    dentales—, así que quedarse con las que CIMA declara para este registro es lo que evita
    servir la financiación del fármaco equivocado. Da igual entonces por qué componente se
    pregunte, lo que hace segura la búsqueda por componentes.

    Y se pregunta por principio activo en vez de por CN porque **cada consulta descarga un Excel
    completo**: por CN serían tantas descargas como presentaciones, cuando una sola trae todas.
    Por eso también se para en cuanto no quedan CN pendientes.

    Un fallo en un componente no descarta las presentaciones reales ya obtenidas de otro: los
    que fallan se acumulan aparte en vez de abortar el bucle.
    """
    resultado = Consulta({}, [], [])
    pendientes = set(cns)

    for nombre in componentes:
        if not pendientes:
            break

        try:
            financiacion = _financiacion_de(nombre)
        except BifimedUnavailable as exc:
            logger.warning("BIFIMED no disponible para %s: %s", nombre, exc)
            resultado.fallidos.append(nombre)
            continue

        encontradas = {
            cn: pres.to_dict()
            for cn in cns
            if cn in pendientes
            and financiacion
            and (pres := financiacion.get_by_cn(cn)) is not None
        }
        if not encontradas:
            logger.warning("BIFIMED %s: ninguna presentación de esta ficha", nombre)
            continue

        logger.info("BIFIMED %s: %d presentaciones de esta ficha", nombre, len(encontradas))
        resultado.presentaciones.update(encontradas)
        pendientes -= set(encontradas)
        resultado.aportaron.append(nombre)

    if resultado.fallidos and not resultado.presentaciones:
        raise BifimedUnavailable(
            f"Ninguna de las {len(resultado.fallidos)} consultas se pudo completar"
        )

    return resultado


def _no_consultado(motivo: str) -> dict[str, Any]:
    return {"status": "not_queried", "reason": motivo, "presentations": []}


def _sin_presentaciones(estado: str, pa: str, motivo: str) -> dict[str, Any]:
    return {
        "status": estado,
        "source": "BIFIMED",
        "active_ingredient_queried": pa,
        "reason": motivo,
        "presentations": [],
    }


def enrich_ft_with_bifimed(
    clinical_json: dict[str, Any],
) -> dict[str, Any]:
    """Enriquece un JSON de ficha técnica con datos BIFIMED.

    Consulta por los componentes del principio activo y **se queda solo con las presentaciones
    cuyo código nacional declara CIMA para este registro**, que es lo que hace que la
    financiación sea del medicamento de esta ficha y no de otro que comparta la molécula.

    **Sin código nacional no se atribuye nada.** Es la única forma de identificar una
    presentación sin ambigüedad: buscar por principio activo y quedarse con todo lo que vuelve
    llevó a servir la financiación de 87 anestésicos dentales como si fuera la de una jeringa de
    epinefrina, con su cita impecable. Un JSON sin `national_codes` se queda en `not_queried`,
    que dice la verdad, en vez de en un bloque de presentaciones ajenas.

    **BIFIMED indexa por la DCI base, no por la forma salina**: `MONTELUKAST` devuelve 99
    presentaciones y `MONTELUKAST SODICO` ninguna. La ingesta desde CIMA resuelve eso en origen
    porque toma el nombre normalizado (`vtm`), no el de la sección 2 de la ficha.

    Args:
        clinical_json: JSON clínico de una ficha técnica, con `national_codes`.

    Returns:
        El mismo JSON con el bloque `reimbursement` añadido.
    """
    if not settings.BIFIMED_ENABLED:
        clinical_json["reimbursement"] = _no_consultado("Enriquecimiento BIFIMED deshabilitado")
        return clinical_json

    cns = [str(c).strip() for c in (clinical_json.get("national_codes") or []) if str(c).strip()]
    if not cns:
        logger.warning("JSON sin national_codes: no hay atribución segura, no se consulta BIFIMED")
        clinical_json["reimbursement"] = _no_consultado(
            "Sin código nacional no se puede atribuir la financiación a este medicamento"
        )
        return clinical_json

    pa = str(clinical_json.get("active_ingredient") or "")
    componentes = _componentes_de(pa)
    if not componentes:
        logger.warning("No se encontró principio_activo en el JSON. No se puede consultar BIFIMED.")
        clinical_json["reimbursement"] = _no_consultado(
            "No se pudo extraer el principio activo del documento"
        )
        return clinical_json

    try:
        consulta = _consultar(componentes, cns)

    except BifimedUnavailable as exc:
        logger.warning("BIFIMED no disponible para %s: %s", pa, exc)
        clinical_json["reimbursement"] = _sin_presentaciones(
            "unavailable", pa, "No se pudo completar la consulta a BIFIMED"
        )
        return clinical_json

    except Exception:
        logger.exception("Error inesperado consultando BIFIMED para %s", pa)
        clinical_json["reimbursement"] = _sin_presentaciones(
            "error", pa, "Error inesperado al consultar BIFIMED"
        )
        return clinical_json

    if not consulta.presentaciones:
        clinical_json["reimbursement"] = _sin_presentaciones(
            "no_data",
            pa,
            f"Ninguna presentación de esta ficha aparece en BIFIMED bajo '{pa}'",
        )
        return clinical_json

    no_encontrados = sorted(set(cns) - set(consulta.presentaciones))
    bloque: dict[str, Any] = {
        "status": "partially_queried" if consulta.fallidos or no_encontrados else "queried",
        "source": "BIFIMED",
        "queried_by": "active_ingredient_filtered_by_national_code",
        "active_ingredient_queried": ", ".join(consulta.aportaron),
        "n_presentations": len(consulta.presentaciones),
        "presentations": list(consulta.presentaciones.values()),
    }
    if consulta.fallidos:
        bloque["not_queried"] = consulta.fallidos
    if no_encontrados:
        bloque["national_codes_not_found"] = no_encontrados
    clinical_json["reimbursement"] = bloque

    return clinical_json
