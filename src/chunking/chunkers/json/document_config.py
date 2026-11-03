"""Define planes y reglas de chunking para cada tipo documental JSON.

Cada configuración selecciona el árbol semántico que organiza los campos,
las listas que no se deben dividir por elementos y los campos que se copian
como contexto a los metadatos de todos los chunks.
"""

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
    """Reglas inmutables de chunking asociadas a un tipo de documento.

    Attributes:
        plan: Árbol semántico de campos y subdivisiones del documento.
        indivisible_list_paths: Rutas relativas a la raíz cuyas listas se
            mantienen completas aunque superen el tamaño objetivo.
        context_fields: Rutas de valores que se incluyen en los metadatos de
            cada chunk, sin agregarlos al texto vectorizado.
    """

    plan: SplitNode
    indivisible_list_paths: frozenset[str]
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
    """Devuelve las reglas de chunking para un tipo documental admitido.

    Args:
        document_type: Clave de tipo documental, por ejemplo ``"paper"``,
            ``"guideline"`` o ``"ficha_tecnica"``.

    Raises:
        ValueError: Si no hay una configuración para ese tipo. El mensaje
            incluye los tipos admitidos.
    """
    try:
        return DOCUMENT_CONFIGS[document_type]
    except KeyError as error:
        available_types = ", ".join(DOCUMENT_CONFIGS)
        raise ValueError(
            f"Tipo de documento no soportado: {document_type!r}. "
            f"Tipos disponibles: {available_types}"
        ) from error
