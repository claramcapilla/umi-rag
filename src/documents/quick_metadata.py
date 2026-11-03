"""Extracción rápida de metadatos al subir (sin LLM).

Best-effort: DOI del texto de las primeras páginas y título de los metadatos del PDF.
El pipeline completo (`src.document_processor`) refina/sobreescribe estos datos después.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO

from pdfminer.high_level import extract_text
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1

from document_processor.clinical.doc_type_detector import detect_document_type

logger = logging.getLogger(__name__)

_DOI_CORE = r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"
_DOI_RE = re.compile(_DOI_CORE, re.IGNORECASE)
# El DOI propio suele ir etiquetado ("doi: 10…", "https://doi.org/10…"); priorizarlo evita
# quedarse con el primer "10." del texto, que puede ser el de una referencia citada.
_DOI_LABELED_RE = re.compile(r"(?:doi\s*:?\s*|doi\.org/)(" + _DOI_CORE + ")", re.IGNORECASE)
_DOI_TRAIL = ".,;:)]}>\"'"


def _clean_doi(raw: str) -> str:
    """Recorta la basura que la extracción de PDF pega al DOI.

    Al perder los espacios, el texto pega la palabra siguiente ("10.1093/abc123Introduction").
    Los sufijos de DOI son casi siempre minúsculas, dígitos y signos, así que recortar una
    palabra en CamelCase pegada al final arregla el caso común sin tocar los DOI normales.
    """
    doi = raw.strip().rstrip(_DOI_TRAIL)
    doi = re.sub(r"(?<=[a-z0-9])(?:[A-Z][a-z]{3,})+$", "", doi)
    return doi.rstrip(_DOI_TRAIL)


def _detect_doc_type(text: str) -> str:
    """Clasifica el tipo por heurística (regex, sin LLM/tokens). `''` si no se puede.

    El detector habla ya el vocabulario único del sistema (`DocVariant`), cuyos valores
    coinciden con los de `DocType`, así que no hay nada que traducir.

    Se mantiene la guarda porque este módulo es best-effort y corre **dentro de la
    transacción de subida**: un fallo aquí no debe tumbar la ingesta de un PDF, solo dejar el
    tipo sin determinar para que el pipeline lo refine después.
    """
    try:
        return detect_document_type(text)
    except Exception as exc:  # noqa: BLE001 - metadato opcional, la subida manda
        logger.debug("quick_metadata: fallo detectando el tipo: %s", exc)
        return ""


def find_doi(text: str) -> str | None:
    """DOI del propio documento: primero el etiquetado, si no el primer `10.` del texto.

    El primer `10.` a secas puede ser el DOI de una referencia citada; el DOI propio suele ir
    tras "doi"/"doi.org" y cerca del principio, así que se intenta ese antes.
    """
    if not text:
        return None
    m = _DOI_LABELED_RE.search(text)
    if m:
        return _clean_doi(m.group(1)) or None
    m = _DOI_RE.search(text)
    if not m:
        return None
    return _clean_doi(m.group(0)) or None


def _xmp_doi(pdf_bytes: bytes) -> str | None:
    """DOI de los metadatos XMP del PDF (p.ej. `prism:doi`, `dc:identifier`)."""
    try:
        parser = PDFParser(BytesIO(pdf_bytes))
        catalog = PDFDocument(parser).catalog
        meta = resolve1(catalog.get("Metadata"))
        if meta is None:
            return None
        xmp = meta.get_data().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - best-effort sobre PDFs arbitrarios
        logger.debug("quick_metadata: fallo leyendo XMP: %s", exc)
        return None
    return find_doi(xmp)


def clean_title(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        # Los títulos de PDF suelen venir en UTF-16 (con BOM). Decodificarlos como UTF-8 dejaría
        # los bytes NUL intercalados ("\x00T\x00í\x00t..."), y Postgres rechaza el NUL (0x00) en
        # campos de texto, lo que tumbaba la ingesta del PDF entero.
        if raw[:2] in (b"\xfe\xff", b"\xff\xfe"):
            title = raw.decode("utf-16", "ignore")
        else:
            title = raw.decode("utf-8", "ignore")
    else:
        title = str(raw)
    title = title.replace("\x00", "").strip()
    if len(title) < 5 or "untitled" in title.lower():
        return None
    return title[:512]


def extract_quick_metadata(pdf_bytes: bytes) -> dict[str, str]:
    """Devuelve {title?, doi?, doc_type?} con lo que se pueda extraer sin LLM."""
    out: dict[str, str] = {}

    text = ""
    try:
        text = extract_text(BytesIO(pdf_bytes), maxpages=3) or ""
    except Exception as exc:  # noqa: BLE001 - best-effort sobre PDFs arbitrarios/corruptos
        logger.debug("quick_metadata: fallo extrayendo texto: %s", exc)

    # El DOI de los metadatos XMP es el del PROPIO documento; se prueba antes que el texto,
    # cuyo primer "10." puede ser el de una referencia citada.
    doi = _xmp_doi(pdf_bytes)
    if not doi and text:
        doi = find_doi(text)
    if doi:
        out["doi"] = doi

    if text.strip():
        doc_type = _detect_doc_type(text)
        if doc_type:
            out["doc_type"] = doc_type

    try:
        parser = PDFParser(BytesIO(pdf_bytes))
        info = PDFDocument(parser).info or []
        for entry in info:
            title = clean_title(entry.get("Title"))
            if title:
                out["title"] = title
                break
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.debug("quick_metadata: fallo extrayendo título: %s", exc)

    # Red de seguridad: ningún valor puede llevar un NUL (0x00) a un campo de texto de Postgres,
    # que lo rechaza y tumbaría la subida. `clean_title` ya lo cubre para el título; esto blinda
    # también a doi/doc_type ante cualquier fuente rara.
    return {k: v.replace("\x00", "") for k, v in out.items()}
