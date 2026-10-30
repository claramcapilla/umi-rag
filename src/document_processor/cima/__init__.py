"""Ingesta de fichas técnicas desde CIMA/AEMPS, sin LLM."""

from .builder import SECCION_A_CAMPO, build_ficha_from_cima
from .client import NREGISTRO, CimaClient, get_cima_base_url
from .errors import CimaSinFicha, CimaUnavailable

__all__ = [
    "NREGISTRO",
    "SECCION_A_CAMPO",
    "CimaClient",
    "CimaSinFicha",
    "CimaUnavailable",
    "build_ficha_from_cima",
    "get_cima_base_url",
]
