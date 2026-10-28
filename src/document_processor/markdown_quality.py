"""Patrones y predicados compartidos para Markdown extraído de documentos."""

from __future__ import annotations

import re
from collections.abc import Sequence

from document_processor.config import HEADING_RE

CAPTION_HEADING_RE = re.compile(
    r"^\s*(?:table|tabla|figure|fig(?:ure)?\.?|figura|cuadro|box|recuadro)\b",
    re.IGNORECASE,
)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*\d+\s*-->")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")
_SPACED_LETTERS_RE = re.compile(r"(?:\w\s){3,}\w")


def table_cells(line: str) -> list[str]:
    """Separa las celdas de una fila Markdown sin dividir barras escapadas."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith(r"\|"):
        row = row[:-1]
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", row)]


def is_table_separator(line: str) -> bool:
    """Indica si una fila Markdown es el separador entre cabecera y datos."""
    cells = table_cells(line)
    return bool(cells) and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def is_layout_table(rows: Sequence[Sequence[str]]) -> bool:
    """Reconoce tablas de maquetación con contenido repartido en varias columnas."""
    return len(rows) > 1 and sum(sum(bool(cell.strip()) for cell in row) >= 2 for row in rows) >= 2


def is_suspicious_heading(text: str) -> bool:
    """Detecta encabezados que parecen artefactos de OCR o texto espaciado."""
    stripped = text.strip()
    letters = sum(char.isalpha() for char in stripped)
    if not stripped or letters < 3:
        return True
    return bool(
        _SPACED_LETTERS_RE.fullmatch(stripped)
        or re.fullmatch(r"[\W\d_]+", stripped)
        or letters / len(stripped) < 0.25
    )


__all__ = [
    "CAPTION_HEADING_RE",
    "HEADING_RE",
    "LIST_ITEM_RE",
    "PAGE_MARKER_RE",
    "TABLE_ROW_RE",
    "is_layout_table",
    "is_suspicious_heading",
    "is_table_separator",
    "table_cells",
]
