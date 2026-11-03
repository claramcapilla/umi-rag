"""Configuración del chunking JSON por tipo documental."""

# ============================================================
# IMPORTS
# ============================================================

from dataclasses import dataclass

from chunking.chunkers.json.JSON_chunker import SplitNode
from chunking.chunkers.json.document_plan_ficha_tecnica import DOCUMENT_PLAN_FC
from chunking.chunkers.json.document_plan_guideline import DOCUMENT_PLAN_GL
from chunking.chunkers.json.document_plan_paper import DOCUMENT_PLAN_PAPER


# ============================================================
# CONFIGURACIÓN POR TIPO DOCUMENTAL
# ============================================================

@dataclass(frozen=True)
class DocumentConfig:
    # Árbol que define cómo se agrupan semánticamente los campos del documento.
    plan: SplitNode
    # Rutas de listas que deben mantenerse completas, aunque excedan el tamaño objetivo.
    indivisible_list_paths: frozenset[str]
    # Rutas cuyos valores se copian al contexto de todos los chunks del documento.
    context_fields: tuple[str, ...]


DOCUMENT_CONFIGS = {
    "paper": DocumentConfig(
        plan=DOCUMENT_PLAN_PAPER,
        indivisible_list_paths=frozenset({"authors", "keywords"}),
        context_fields=("doi", "title", "journal", "year"),
    ),
    "guideline": DocumentConfig(
        plan=DOCUMENT_PLAN_GL,
        indivisible_list_paths=frozenset(
            {"toc", "references", "abbreviations", "metadata.authors"}
        ),
        context_fields=(
            "metadata.title",
            "metadata.organization",
            "metadata.version",
        ),
    ),
    "ficha_tecnica": DocumentConfig(
        plan=DOCUMENT_PLAN_FC,
        indivisible_list_paths=frozenset(
            {"excipients", "national_codes", "national_codes_queried"}
        ),
        context_fields=(
            "medicine_name",
            "pharmaceutical_form",
            "active_ingredient",
            "strength",
        ),
    ),
}


# ============================================================
# OBTENER CONFIGURACIÓN
# ============================================================

def get_document_config(document_type: str) -> DocumentConfig:
    """Devuelve la configuración de chunking para el tipo documental."""
    try:
        return DOCUMENT_CONFIGS[document_type]
    except KeyError as error:
        available_types = ", ".join(DOCUMENT_CONFIGS)
        raise ValueError(
            f"Tipo de documento no soportado: {document_type!r}. "
            f"Tipos disponibles: {available_types}"
        ) from error
