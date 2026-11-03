"""Extracción de metadatos bibliográficos y tipo desde el JSON clínico rico.

El JSON lo produce `src.document_processor` con forma **plana** (los campos
de cada sección se fusionan al nivel superior; no hay claves `metadata`/
`identification`/`metadata_and_scope` de envoltorio salvo en la guía clínica, cuya
sección de metadatos sí anida un dict `metadata`). Ver docs/RAG_LLM_GUIDELINES.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from django.utils import timezone

from documents.models import DocType, SourceDocument

logger = logging.getLogger(__name__)

_TITULO_MAX: int = SourceDocument._meta.get_field("title").max_length or 512
"""Tope del campo, porque el título de una ficha ya no lo acota un LLM.

La sección 1 de un SmPC multiconcentración **lista todas las presentaciones**: la de Eutirox, con
doce, mide 461 caracteres, al 90% del límite. Django no valida `max_length` en `save()`, así que
pasarse era un `DataError` en PostgreSQL —después de escribir el PDF y gastar todas las peticiones
al registro— y un guardado silencioso en el SQLite de desarrollo y de CI, o sea un fallo que ningún
gate podía ver.
"""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if value:
        return [str(value)]
    return []


def extract_metadata(clinical_json: dict[str, Any]) -> dict[str, Any]:
    """Devuelve campos para SourceDocument a partir del JSON clínico."""

    # Ficha técnica de CIMA
    if {
        "medicine_name",
        "composition",
        "pharmaceutical_form",
        "indications",
        "posology",
    }.issubset(clinical_json):
        return _from_ficha_tecnica(clinical_json)

    # Guía clínica
    document_type = str(clinical_json.get("document_type") or "").lower()

    if document_type in {"guideline", "clinical_guideline", "guia_clinica"}:
        return _from_guideline(clinical_json)

    # Artículo científico
    return _from_paper(clinical_json)


def _normalize_doi(raw: Any) -> str:
    """DOI en forma canónica (bare): sin prefijo de resolver ni «doi:».

    El LLM a veces devuelve el DOI como URL completa («https://doi.org/10.1002/art.41095»)
    y a veces desnudo («10.1002/art.41092»). La biblioteca y el enriquecimiento esperan la
    forma desnuda (y la plantilla ya antepone `https://doi.org/`), así que se normaliza aquí,
    de forma determinista, en vez de depender de que el modelo sea consistente.
    """
    doi = str(raw or "").strip()
    for prefijo in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if doi.lower().startswith(prefijo):
            doi = doi[len(prefijo) :]
            break
    return doi.strip()


def _from_paper(cj: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_type": DocType.PAPER,
        "title": str(cj.get("title") or ""),
        "authors": _as_list(cj.get("authors")),
        "journal": str(cj.get("journal") or ""),
        "year": str(cj.get("year") or ""),
        "doi": _normalize_doi(cj.get("doi")),
        "pmid": "",
    }


def _fecha_de_revision(smpc_date: Any) -> str:
    """Fecha de revisión del SmPC en ISO, desde el epoch en milisegundos que sirve CIMA.

    **Se convierte a la zona del proyecto, no a UTC.** CIMA da el instante en hora de Madrid y
    `TIME_ZONE` es `Europe/Madrid`: leerlo como UTC dejaba la fecha un día antes en 2 de las 5
    fichas del corpus versionado (73187 y 75831). En el campo que `CLAUDE.md` llama material para
    saber si lo citado sigue vigente, un día menos es una cita equivocada.

    Un epoch fuera de rango **lanza `ValueError`**, no devuelve algo raro: comprobado, tanto con un
    negativo grande («year -1199 is out of range») como con un entero desmesurado. Y este valor lo
    da un tercero, así que un campo corrupto tumbaría la ingesta entera —sin clasificar, y después
    de haber gastado todas las peticiones al registro— por una fecha de cita. Sin fecha se cita
    igual, solo peor, así que se degrada en vez de fallar.
    """
    if not isinstance(smpc_date, int):
        return ""
    try:
        instante = datetime.fromtimestamp(smpc_date / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        logger.warning("CIMA devolvió una fecha de revisión fuera de rango: %r", smpc_date)
        return ""
    return timezone.localdate(instante).isoformat()


def _from_ficha_tecnica(cj: dict[str, Any]) -> dict[str, Any]:
    """Metadatos de la ficha, con la cita compuesta desde el bloque `_source` de CIMA.

    Antes la cita de cada chunk de una ficha salía como «Unknown - nombre . titular»: la regla
    innegociable de citar la fuente servida en su versión más débil, y justo en el documento más
    normativo del corpus. Y **para un SmPC la fecha de revisión es material**, porque sin ella no
    hay forma de saber si lo citado sigue vigente; es además el requisito de atribución de la
    reutilización de información del sector público.

    El titular pasa a ocupar el hueco de autor —es quien responde del texto— y la fuente
    autoritativa, con número de registro y fecha, va donde `_build_source` pone la publicación.
    `year` se queda con el año a secas para no cambiar lo que significa ese campo en la
    biblioteca. Sin `_source` —las fichas del corpus congelado, generadas por el camino viejo— se
    mantiene el comportamiento anterior en vez de inventar procedencia.
    """
    fuente = cj.get("_source")
    titular = str(cj.get("marketing_authorisation_holder") or "")
    if not isinstance(fuente, dict):
        return {
            "doc_type": DocType.FICHA_TECNICA,
            "title": str(cj.get("medicine_name") or cj.get("active_ingredient") or ""),
            "authors": [],
            "journal": titular,
            "year": "",
            "doi": "",
            "pmid": "",
        }

    revisada = _fecha_de_revision(fuente.get("smpc_date"))
    titulo = str(cj.get("medicine_name") or cj.get("active_ingredient") or "")[:_TITULO_MAX]
    publicacion = ["Ficha técnica AEMPS"]
    if fuente.get("registration_number"):
        publicacion.append(f"Nº registro {fuente['registration_number']}")
    if revisada:
        publicacion.append(f"revisada {revisada}")
    return {
        "doc_type": DocType.FICHA_TECNICA,
        "title": titulo,
        "authors": [titular] if titular else [],
        "journal": " · ".join(publicacion),
        "year": revisada[:4],
        "doi": "",
        "pmid": "",
    }


def _author_names(authors: Any) -> list[str]:
    """La guía anida autores como [{name, role, affiliation}]; extrae los nombres."""
    if not isinstance(authors, list):
        return _as_list(authors)
    names: list[str] = []
    for author in authors:
        if isinstance(author, dict):
            name = author.get("name")
            if name:
                names.append(str(name))
        elif author:
            names.append(str(author))
    return names


def _from_guideline(cj: dict[str, Any]) -> dict[str, Any]:
    m = cj.get("metadata", {}) or {}
    return {
        "doc_type": DocType.GUIA_CLINICA,
        "title": str(m.get("title") or ""),
        "authors": _author_names(m.get("authors")),
        "journal": str(m.get("organization") or ""),
        "year": str(m.get("year") or ""),
        # Muchas guías de sociedad se publican con DOI. El LLM lo extrae del propio documento
        # (mejor que el regex de subida, que no distingue el DOI propio del de una cita); el
        # enriquecimiento lo usa luego para resolver PubMed. Antes se fijaba a "" y se perdía.
        "doi": _normalize_doi(m.get("doi")),
        "pmid": "",
    }
