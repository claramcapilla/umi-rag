"""
Módulo BIFIMED para consultar datos de financiación de
medicamentos del Ministerio de Sanidad de España.

Permite buscar por principio activo o código nacional (CN)
y obtener información de financiación por presentación.
"""

from .client import BifimedClient
from .errors import BifimedUnavailable
from .models import (
    IndicacionFinanciacion,
    MedicamentoFinanciacion,
    PresentacionFinanciacion,
)

__all__ = [
    "BifimedClient",
    "BifimedUnavailable",
    "IndicacionFinanciacion",
    "MedicamentoFinanciacion",
    "PresentacionFinanciacion",
]
