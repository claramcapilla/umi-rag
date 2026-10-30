"""Qué documento de un medicamento de CIMA es su ficha técnica, y si es ingerible.

El contrato de CIMA —`docs` con `tipo` 1 es la ficha técnica, y solo la segmentada (`secc`) es
ingerible desde la retirada del camino PDF+LLM— se decide aquí, en un módulo sin dependencias, para
que el recorrido, los candidatos de la búsqueda y el constructor no lo codifiquen cada uno por su
cuenta y deriven si AEMPS cambia la numeración.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TIPO_FICHA_TECNICA = 1


def documento_de_ficha(medicamento: Mapping[str, Any]) -> dict[str, Any] | None:
    """El documento de ficha técnica del medicamento (`docs` con `tipo` 1), o None."""
    documentos: list[dict[str, Any]] = medicamento.get("docs") or []
    return next((d for d in documentos if d.get("tipo") == _TIPO_FICHA_TECNICA), None)


def ficha_segmentada(medicamento: Mapping[str, Any]) -> dict[str, Any] | None:
    """El documento de ficha técnica segmentada e ingerible, o None si no la hay."""
    ficha = documento_de_ficha(medicamento)
    return ficha if ficha and ficha.get("secc") else None
