from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from document_processor.config import PDF_CHUNKING_CONVERT_TIMEOUT_SECONDS

from .chunking_markdown import normalize_for_chunking
from .markdown_cleaner import clean_markdown
from .pdf_converter import ConversionResult, ConverterConfig, convert_pdf
from .redaction import redact_personal_data

PAGE_MARKER = "<!-- page: %page-number% -->"


@dataclass(frozen=True)
class ChunkingMarkdown:
    markdown: str
    pages: int


def get_hybrid_url() -> str:
    url: str = settings.CHUNKING_HYBRID_URL
    if not url:
        raise ImproperlyConfigured("CHUNKING_HYBRID_URL no está definida.")
    allowed: list[str] = list(settings.CHUNKING_HYBRID_ALLOWED_ENDPOINTS)
    if url not in allowed:
        raise ImproperlyConfigured(
            f"CHUNKING_HYBRID_URL={url!r} no está en CHUNKING_HYBRID_ALLOWED_ENDPOINTS "
            f"({allowed!r}). Añádelo explícitamente si de verdad quieres enviarle los PDF."
        )
    return url


def convert_pdf_for_chunking(pdf_path: str | Path) -> ConversionResult:
    return convert_pdf(
        pdf_path,
        ConverterConfig(
            markdown_page_separator=PAGE_MARKER,
            with_layout=True,
            hybrid_url=get_hybrid_url(),
            timeout_seconds=PDF_CHUNKING_CONVERT_TIMEOUT_SECONDS,
        ),
    )


def markdown_for_chunking(pdf_path: str | Path) -> ChunkingMarkdown:
    result = convert_pdf_for_chunking(pdf_path)
    normalized = normalize_for_chunking(clean_markdown(result.markdown), result.layout)
    return ChunkingMarkdown(markdown=redact_personal_data(normalized), pages=result.n_pages)