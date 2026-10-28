"""Esquemas de extracción y metadatos de procedencia."""

from pathlib import Path
from typing import Final

from document_processor.schema import DocVariant, NON_DOCUMENT_PROVENANCE, Provenance

from .ficha_tecnica import SECTIONS_FT
from .guia_clinica import SECTIONS_CG
from .paper import SECTIONS_PAPER

SECTIONS_BY_VARIANT: Final = {
    "paper": SECTIONS_PAPER,
    "ficha_tecnica": SECTIONS_FT,
    "guia_clinica": SECTIONS_CG,
}
VARIANTES_CON_PROMPT: Final = ("paper", "guia_clinica")
PROMPTS_DIR: Final = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_BY_VARIANT: Final = {
    "paper": "paper.txt",
    "guia_clinica": "guia_clinica.txt",
}


def system_prompt_path(variant: DocVariant) -> Path:
    """Devuelve la ruta del prompt de sistema de la variante indicada."""
    try:
        filename = _PROMPT_BY_VARIANT[variant]
    except KeyError as exc:
        raise ValueError(f"La variante {variant!r} no tiene un prompt de sistema.") from exc
    path = PROMPTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"No existe el prompt de sistema requerido: {path}")
    return path


def field_provenance(variant: DocVariant, path: str) -> Provenance:
    """Devuelve la procedencia declarada para un campo o una ruta anidada."""
    if path in NON_DOCUMENT_PROVENANCE:
        return NON_DOCUMENT_PROVENANCE[path]

    root_field = path.partition(".")[0]
    if root_field in NON_DOCUMENT_PROVENANCE:
        return NON_DOCUMENT_PROVENANCE[root_field]
    sections = SECTIONS_BY_VARIANT[variant]
    if any(root_field in section["fields"] for section in sections.values()):
        return "document"
    raise KeyError(f"El campo {path!r} no está declarado para {variant!r}.")


def verbatim_fields(variant: DocVariant) -> frozenset[str]:
    """Devuelve las rutas que el esquema exige copiar literalmente."""
    return frozenset(
        path
        for section in SECTIONS_BY_VARIANT[variant].values()
        for path in section.get("verbatim", [])
    )


__all__ = [
    "PROMPTS_DIR",
    "SECTIONS_BY_VARIANT",
    "VARIANTES_CON_PROMPT",
    "field_provenance",
    "system_prompt_path",
    "verbatim_fields",
]