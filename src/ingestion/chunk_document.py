"""Convierte un JSON clínico en chunks normalizados para el RAG."""

# ============================================================
# IMPORTS
# ============================================================

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

# Chunking de documentos clínicos
from chunking.utilities import create_tokenizer, create_token_counter
from chunking.chunkers.json import (
    HierarchicalJSONChunker,
    get_document_config,
)
from chunking.config import load_config


# ============================================================
# RUTAS Y CONFIGURACIÓN
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "configs" / "chunker_config.json"


# ============================================================
# GENERAR CHUNKS CLÍNICOS
# ============================================================

def chunk_clinical_document(
    clinical_json: dict[str, Any],
    document_id: str,
    document_type: str,
    source_file: str = "",
) -> list[dict[str, Any]]:
    """Procesa un documento con el chunker jerárquico existente."""

    # ========================================================
    # CARGAR CONFIGURACIÓN DEL CHUNKER
    # ========================================================

    config = load_config(CONFIG_FILE)
    tokenizer_config = config["tokenizer"]

    model_name = tokenizer_config["model_name"]
    target_tokens = int(tokenizer_config["target_tokens"])
    max_tokens = int(tokenizer_config["max_tokens"])
    add_special_tokens = tokenizer_config.get("add_special_tokens", True)

    document_config = get_document_config(document_type)

    # ========================================================
    # PREPARAR EL CONTADOR DE TOKENS
    # ========================================================

    tokenizer = create_tokenizer(model_name)
    token_counter = create_token_counter(
        tokenizer,
        add_special_tokens=add_special_tokens,
    )

    # ========================================================
    # CONFIGURAR Y EJECUTAR EL CHUNKER
    # ========================================================

    chunker = HierarchicalJSONChunker(
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        token_counter=token_counter,
        document_plan=document_config.plan,
        root_field=None,
        indivisible_list_paths=set(document_config.indivisible_list_paths),
        context_fields=list(document_config.context_fields),
    )

    chunks = chunker.chunk_document(clinical_json)

    # ========================================================
    # NORMALIZAR LOS REGISTROS PARA INDEXACIÓN
    # ========================================================

    normalized = []

    for index, chunk in enumerate(chunks):
        record = asdict(chunk)

        metadata = dict(record.get("metadata") or {})

        for key in (
            "block",
            "group_name",
            "level",
            "json_paths",
            "exceeds_target_limit",
            "exceeds_max_limit",
        ):
            if key in record:
                metadata[key] = record[key]

        if record.get("chunk_id") is not None:
            metadata["original_chunk_id"] = record["chunk_id"]

        normalized.append({
            "chunk_id": f"json__{document_id}__JC__{index:06d}",
            "text": record["text"],
            "format": "json",
            "document_type": document_type,
            "document_id": document_id,
            "source_file": source_file,
            "source_path": source_file,
            "strategy": "JSONChunking",
            "strategy_abbreviation": "JC",
            "strategy_params": {
                "target_tokens": target_tokens,
                "max_tokens": max_tokens,
            },
            "chunk_index": index,
            "token_count": record.get("token_count"),
            "metadata": metadata,
        })

    return normalized
