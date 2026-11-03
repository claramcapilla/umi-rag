"""Indexación individual de documentos clínicos en Qdrant."""

# ============================================================
# IMPORTS
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import Any

# Chunking e indexación de documentos clínicos
from ingestion.chunk_document import chunk_clinical_document
from indexing.pipeline import DEFAULT_CONFIG, index_chunks


# ============================================================
# INDEXAR DOCUMENTO CLÍNICO
# ============================================================

def index_clinical_document(
    clinical_json: dict[str, Any],
    document_id: str,
    document_type: str,
    source_file: str = "",
    previous_ids: list[str] | None = None,
    config_file: Path = DEFAULT_CONFIG,
    specialties: list[str] | None = None,
) -> dict[str, Any]:
    """Indexa un documento con el chunking y los embeddings de umi-rag."""

    # ========================================================
    # GENERAR CHUNKS
    # ========================================================

    chunks = chunk_clinical_document(
        clinical_json=clinical_json,
        document_id=document_id,
        document_type=document_type,
        source_file=source_file,
    )

    # ========================================================
    # ASIGNAR ESPECIALIDADES
    # ========================================================

    specialties = list(dict.fromkeys(specialties or []))

    if not specialties:
        raise ValueError(
            "El documento debe pertenecer al menos a una especialidad."
        )

    base_chunks = chunks
    chunks = []

    for specialty in specialties:
        for chunk in base_chunks:
            new_chunk = dict(chunk)
            new_chunk["chunk_id"] = f"{chunk['chunk_id']}__{specialty}"
            new_chunk["document_id"] = str(document_id)
            new_chunk["specialty"] = specialty
            chunks.append(new_chunk)

    # ========================================================
    # VALIDAR CHUNKS
    # ========================================================

    if not chunks:
        raise ValueError("El documento no ha generado ningún chunk.")

    if len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise ValueError("Se han generado chunk_id duplicados.")

    # ========================================================
    # DELEGAR LA INDEXACIÓN
    # ========================================================

    result = index_chunks(
        chunks,
        config_file=config_file,
        previous_ids=previous_ids,
    )

    # ========================================================
    # DEVOLVER RESULTADO
    # ========================================================

    return {
        "document_id": document_id,
        "chunk_count": len(chunks),
        **result,
    }
