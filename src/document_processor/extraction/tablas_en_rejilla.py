from __future__ import annotations

import re
from pathlib import Path

from document_processor.config import PDF_CHUNKING_CONVERT_TIMEOUT_SECONDS

from .chunking_conversion import get_hybrid_url
from .markdown_cleaner import clean_markdown
from .pdf_converter import ConverterConfig, convert_pdf
from .redaction import redact_personal_data

TITULO_DE_TABLA_RE = re.compile(r"\b(?:Table|TABLE|Tabla|TABLA)\s+[0-9IVX]+(?:\.[0-9]+)*")


def bloques_de_tablas(md: str) -> str:
    bloques: list[str] = []
    actual: list[str] = []
    previa = ""
    for linea in [*md.split("\n"), ""]:
        if linea.lstrip().startswith("|"):
            actual.append(linea)
            continue
        if actual:
            titulo = f"{previa}\n" if TITULO_DE_TABLA_RE.search(previa) else ""
            bloques.append(titulo + "\n".join(actual))
            actual = []
        if linea.strip():
            previa = linea.strip()
    return "\n\n".join(bloques)


def tablas_en_rejilla(pdf_path: str | Path) -> str:
    conversion = convert_pdf(
        pdf_path,
        ConverterConfig(
            hybrid_url=get_hybrid_url(), timeout_seconds=PDF_CHUNKING_CONVERT_TIMEOUT_SECONDS
        ),
    )
    return redact_personal_data(bloques_de_tablas(clean_markdown(conversion.markdown)))