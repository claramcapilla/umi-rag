"""API pública para segmentación y mapeo de secciones."""

from .chunk_mapper import (
    SectionChunk,
    get_chunk_for_section,
    get_chunk_for_section_cg,
)
from .section_splitter import (
    DocumentSection,
    SectionCategory,
    document_has_structure,
    get_sections_by_category,
    split_document,
)
from .ventanas import partir_en_ventanas

__all__ = [
    "DocumentSection",
    "SectionCategory",
    "SectionChunk",
    "document_has_structure",
    "get_chunk_for_section",
    "get_chunk_for_section_cg",
    "get_sections_by_category",
    "partir_en_ventanas",
    "split_document",
]