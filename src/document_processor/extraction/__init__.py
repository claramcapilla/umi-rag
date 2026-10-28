"""API de extracción documental."""

from .errors import ConversionError, FichaTecnicaNoIngerible
from .markdown_cleaner import clean_markdown
from .pdf_converter import ConversionResult, ConverterConfig, convert_pdf

__all__ = [
    "ConversionError",
    "ConversionResult",
    "ConverterConfig",
    "FichaTecnicaNoIngerible",
    "clean_markdown",
    "convert_pdf",
]