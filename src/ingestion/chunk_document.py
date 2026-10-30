
"""Convierte un JSON clínico en chunks normalizados para el RAG."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from chunking.utilities import create_tokenizer, create_token_counter
from chunking.chunkers.json import (
    HierarchicalJSONChunker,
    DOCUMENT_PLAN_PAPER,
    DOCUMENT_PLAN_GL,
    DOCUMENT_PLAN_FC,
)
from chunking.config import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "configs" / "chunker_config.json"

DOCUMENT_PLANS = {
    "paper": DOCUMENT_PLAN_PAPER,
    "guideline": DOCUMENT_PLAN_GL,
    "ficha_tecnica": DOCUMENT_PLAN_FC,
}

INDIVISIBLE_LIST_PATHS = {
    "paper": ["authors", "keywords"],
    "guideline": ["toc", "references", "abbreviations", "metadata.authors"],
    "ficha_tecnica": ["excipients", "national_codes", "national_codes_queried"],
}


def chunk_clinical_document(
    clinical_json: dict[str, Any],
    document_id: str,
    document_type: str,
    source_file: str = "",
) -> list[dict[str, Any]]:
    """Procesa un documento con el chunker jerárquico existente."""

    # Utilizar la misma configuración que el chunking por lotes.
    config = load_config(CONFIG_FILE)
    tokenizer_config = config["tokenizer"]

    model_name = tokenizer_config["model_name"]
    target_tokens = int(tokenizer_config["target_tokens"])
    max_tokens = int(tokenizer_config["max_tokens"])
    add_special_tokens = tokenizer_config.get("add_special_tokens", True)

    if document_type not in DOCUMENT_PLANS:
        raise ValueError(f"Tipo documental no soportado: {document_type}")

    # Crear el mismo contador de tokens que utiliza el runner original.
    tokenizer = create_tokenizer(model_name)
    token_counter = create_token_counter(
        tokenizer,
        add_special_tokens=add_special_tokens,
    )

    # Reutilizar directamente tu HierarchicalJSONChunker.
    chunker = HierarchicalJSONChunker(
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        token_counter=token_counter,
        document_plan=DOCUMENT_PLANS[document_type],
        root_field=None,
        indivisible_list_paths=set(INDIVISIBLE_LIST_PATHS[document_type]),
    )

    chunks = chunker.chunk_document(clinical_json)

    # Convertir la salida a los registros que espera tu indexador.
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
