"""API de extracción documental.

La conversión de un PDF individual está disponible como ``convert_pdf``.
La conversión de carpetas se mantiene en el módulo ``pdf_converter`` y no se
expone desde el paquete.
"""

from .pdf_converter import ConversionResult, ConverterConfig, convert_pdf

__all__ = ["ConversionResult", "ConverterConfig", "convert_pdf"]