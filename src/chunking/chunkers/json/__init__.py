"""Chunking jerárquico para documentos JSON estructurados.

Reexporta el chunker, sus estructuras de datos y los planes y configuraciones
disponibles para cada tipo documental.
"""

from chunking.chunkers.json.JSON_chunker import (
    Chunk,
    HierarchicalJSONChunker,
    SplitNode,
)
from chunking.chunkers.json.document_config import (
    DOCUMENT_CONFIGS,
    DocumentConfig,
    get_document_config,
)
from chunking.chunkers.json.document_plan_ficha_tecnica import DOCUMENT_PLAN_FC
from chunking.chunkers.json.document_plan_guideline import DOCUMENT_PLAN_GL
from chunking.chunkers.json.document_plan_paper import DOCUMENT_PLAN_PAPER

__all__ = [
    "Chunk",
    "DOCUMENT_CONFIGS",
    "DOCUMENT_PLAN_FC",
    "DOCUMENT_PLAN_GL",
    "DOCUMENT_PLAN_PAPER",
    "DocumentConfig",
    "HierarchicalJSONChunker",
    "SplitNode",
    "get_document_config",
]