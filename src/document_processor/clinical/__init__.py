"""API clínica compartida por el pipeline de extracción."""

from .cifras import Cifra, cifras_que_faltan, claves_del_json
from .contenido import fusionar_salidas, revisar_contenido, ruta_en_bucle
from .doc_type_detector import detect_document_type, guideline_signal_digest
from .idioma import ISO_POR_IDIOMA, detectar_idioma, normalizar_idioma
from .json_sanitizer import safe_parse_llm_json, sanitize_clinical_json
from .json_validator import log_validation_results, validate_clinical_json
from .referencias import renumerar_ids, resolver_referencias
from .tablas import admite_copia_literal, incorporar_tablas

__all__ = [
    "Cifra",
    "ISO_POR_IDIOMA",
    "admite_copia_literal",
    "cifras_que_faltan",
    "claves_del_json",
    "detect_document_type",
    "detectar_idioma",
    "fusionar_salidas",
    "guideline_signal_digest",
    "incorporar_tablas",
    "log_validation_results",
    "normalizar_idioma",
    "renumerar_ids",
    "resolver_referencias",
    "revisar_contenido",
    "ruta_en_bucle",
    "safe_parse_llm_json",
    "sanitize_clinical_json",
    "validate_clinical_json",
]