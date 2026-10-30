
"""Indexación individual de documentos clínicos en Qdrant."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ingestion.chunk_document import chunk_clinical_document
from indexing.pipeline import load_config, DEFAULT_CONFIG, _resolve
from indexing.embedder import create_embedder, resolve_worker_config
from indexing.embedding_cache import embed_chunks_with_cache
from indexing.qdrant_store import (
    create_client,
    ensure_collection,
    upsert_chunks,
    delete_points,
)


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

    # 1. Cargar la configuración de indexación existente.
    config_file = Path(config_file).resolve()
    config = load_config(config_file)
    root = config_file.parent.parent

    embedding_config = config["embeddings"]
    indexing_config = config["indexing"]
    qdrant_config = config["qdrant"]
    paths = config["paths"]

    model_name = embedding_config.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError(
            "Debes configurar embeddings.model_name "
            "en configs/indexing_config.json."
        )

    # 2. Generar los chunks con tu chunker jerárquico.
    chunks = chunk_clinical_document(
        clinical_json=clinical_json,
        document_id=document_id,
        document_type=document_type,
        source_file=source_file,
    )

    # Cada documento puede pertenecer a varias especialidades.
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
            new_chunk["specialty"] = specialty
            chunks.append(new_chunk)

    if not chunks:
        raise ValueError("El documento no ha generado ningún chunk.")

    if len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise ValueError("Se han generado chunk_id duplicados.")

    # 3. Configurar los mismos workers que el indexador original.
    num_workers, cpu_threads, gpu_ids = resolve_worker_config(
        indexing_config
    )

    device_override = (
        "cpu" if gpu_ids or num_workers > 1 else None
    )

    # 4. Crear los embeddings utilizando tu modelo y tu caché.
    embedder = create_embedder(
        embedding_config,
        device_override=device_override,
    )

    vectors = embed_chunks_with_cache(
        embedder=embedder,
        chunks=chunks,
        model_name=model_name,
        cache_dir=_resolve(root, paths["embedding_cache_dir"]),
        prefix=embedding_config.get("document_prefix", ""),
        batch_size=int(indexing_config["cache_batch_size"]),
        use_cache=bool(indexing_config.get("use_cache", True)),
        num_workers=num_workers,
        cpu_threads=cpu_threads,
        gpu_ids=gpu_ids,
        embedding_batch_size=int(indexing_config["embedding_batch_size"]),
    )

    if len(vectors) != len(chunks):
        raise ValueError("No se ha generado un embedding por chunk.")

    # 5. Calcular los UUID que utilizará Qdrant.
    point_ids = [
        str(uuid.uuid5(uuid.NAMESPACE_URL, chunk["chunk_id"]))
        for chunk in chunks
    ]

    # Identificar los puntos que ya no pertenecen al documento.
    stale_ids = list(set(previous_ids or []) - set(point_ids))

    # 6. Conectar con Qdrant e indexar.
    client = create_client(qdrant_config)
    collection_name = qdrant_config["collection_name"]

    try:
        ensure_collection(
            client,
            collection_name,
            vector_size=len(vectors[0]),
        )

        updated_count = upsert_chunks(
            client,
            collection_name,
            chunks,
            vectors,
            batch_size=int(indexing_config["batch_size"]),
            embedding_model=model_name,
        )

        # Borrar puntos antiguos solo después del upsert correcto.
        if stale_ids:
            delete_points(client, collection_name, stale_ids)

    finally:
        client.close()

    # 7. Devolver información para actualizar SourceDocument.
    return {
        "document_id": document_id,
        "chunk_count": len(chunks),
        "qdrant_ids": point_ids,
        "updated_count": updated_count,
        "deleted_count": len(stale_ids),
        "embedding_model": model_name,
        "collection_name": collection_name,
    }
