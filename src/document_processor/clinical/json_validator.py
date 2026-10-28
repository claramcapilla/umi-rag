"""Validación de JSON clínico generado por el LLM.

Verifica que el JSON de salida contenga todas las claves
esperadas según el tipo de documento detectado.
"""

from __future__ import annotations

import logging
from typing import Any

from src.document_processor.schema import NON_DOCUMENT_PROVENANCE, DocVariant
from src.document_processor.sections import SECTIONS_BY_VARIANT

logger = logging.getLogger(__name__)


def validate_clinical_json(
    data: dict[str, Any],
    doc_type: DocVariant,
) -> list[str]:
    """Valida que el JSON contenga los campos esperados.

    `doc_type` es una `DocVariant`: el sistema tiene un solo vocabulario.

    Un campo a `null` es válido (son opcionales por diseño) y solo se registra en debug: lo
    que se devuelve como problema es la **ausencia** de la clave. Los campos extra tampoco
    son problema, y `_usage`, `_tokens` y `financiacion` se ignoran de forma explícita porque
    son internos del pipeline y no del esquema clínico (`financiacion` lo añade el
    enriquecimiento de BiFiMed y no llega al RAG).

    Devuelve la lista de problemas; vacía si todo correcto.
    """
    schema = SECTIONS_BY_VARIANT[doc_type]

    issues: list[str] = []

    for section_info in schema.values():
        fields = section_info["fields"]
        section_name = section_info["name"]

        for field in fields:
            if field not in data:
                issues.append(f"Campo ausente: '{field}' (sección: {section_name})")
            elif data[field] is None:
                logger.debug(
                    "Campo '%s' es null (sección: %s)",
                    field,
                    section_name,
                )

    expected = set()
    for section_info in schema.values():
        expected.update(section_info["fields"])
    internal = {"_tokens", *NON_DOCUMENT_PROVENANCE}

    extra = set(data.keys()) - expected - internal
    if extra:
        logger.debug("Campos extra no definidos en schema: %d", len(extra))

    return issues


def log_validation_results(
    issues: list[str],
    doc_type: DocVariant,
) -> None:
    """Registra los resultados de validación."""
    if not issues:
        logger.info(
            "Validación JSON OK (%s): todos los campos presentes",
            doc_type,
        )
        return

    logger.warning(
        "Validación JSON (%s): %d problemas encontrados",
        doc_type,
        len(issues),
    )
    for issue in issues:
        logger.warning("  - %s", issue)