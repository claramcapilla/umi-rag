"""Orquesta normalización, embeddings en caché y escritura en Qdrant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from umi_rag.indexing.embedder import create_embedder, resolve_worker_config
from umi_rag.indexing.embedding_cache import embed_chunks_with_cache
from umi_rag.indexing.normalizer import load_normalized_chunks, write_normalized_chunks
from umi_rag.indexing.qdrant_store import (
    create_client,
    ensure_collection,
    upsert_chunks,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "indexing_config.json"


def load_config(config_file: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    # Todas las fases leen el mismo archivo para mantener modelo y colección
    # consistentes entre la indexación y las consultas.
    with config_file.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"La configuración debe ser un objeto JSON: {config_file}")
    return config


def _resolve(root: Path, value: str) -> Path:
    # Las rutas relativas de configuración se interpretan desde la raíz
    # del proyecto, no desde el directorio actual del proceso.
    path = Path(value)
    return path if path.is_absolute() else root / path


def run_indexing(config_file: Path = DEFAULT_CONFIG) -> int:
    root = config_file.resolve().parent.parent
    config = load_config(config_file)
    paths = config["paths"]
    embedding_config = config["embeddings"]
    indexing_config = config["indexing"]
    qdrant_config = config["qdrant"]
    normalized_file = _resolve(root, paths["normalized_chunks_file"])
    for key in ("batch_size", "embedding_batch_size", "cache_batch_size"):
        if int(indexing_config[key]) < 1:
            raise ValueError(f"indexing.{key} debe ser al menos 1.")

    # Fase 1: convertir las salidas del chunker a un formato de indexación
    # estable y guardarlo para poder inspeccionarlo o reutilizarlo.
    count = write_normalized_chunks(
        _resolve(root, paths["chunks_dir"]),
        normalized_file,
    )
    print(f"Chunks normalizados: {count} -> {normalized_file}")
    chunks = load_normalized_chunks(normalized_file)
    if len({str(chunk["chunk_id"]) for chunk in chunks}) != len(chunks):
        raise ValueError(
            "Hay chunk_id duplicados en los documentos normalizados. "
            "Cada chunk debe tener un ID estable y único."
        )
    # Se valida antes de cargar el modelo para dar un error claro si todavía
    # no se ha decidido qué embeddings usar.
    model_name = embedding_config.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError(
            "Configura embeddings.model_name en "
            f"{config_file} antes de indexar."
        )

    # Fase 2: vectorizar el texto del documento, aprovechando la caché por
    # modelo, chunk_id y contenido.
    num_workers, cpu_threads, gpu_ids = resolve_worker_config(indexing_config)
    # En modo multiproceso el modelo se carga primero en CPU; Sentence
    # Transformers asigna cada worker al dispositivo configurado.
    device_override = "cpu" if gpu_ids or num_workers > 1 else None
    embedder = create_embedder(embedding_config, device_override=device_override)
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
    vector_size = len(vectors[0])

    # Fase 3: comprobar/crear una única colección y subir los puntos por lotes.
    client = create_client(qdrant_config)
    try:
        ensure_collection(client, qdrant_config["collection_name"], vector_size)
        updated_count = upsert_chunks(
            client,
            qdrant_config["collection_name"],
            chunks,
            vectors,
            int(indexing_config["batch_size"]),
            embedding_model=model_name,
        )
    finally:
        client.close()
    print(f"Chunks nuevos o modificados en Qdrant: {updated_count}/{len(chunks)}")
    return updated_count


def normalize_only(config_file: Path = DEFAULT_CONFIG) -> int:
    # Permite inspeccionar los registros normalizados sin cargar el modelo
    # ni requerir una conexión activa a Qdrant.
    root = config_file.resolve().parent.parent
    config = load_config(config_file)
    paths = config["paths"]
    return write_normalized_chunks(
        _resolve(root, paths["chunks_dir"]),
        _resolve(root, paths["normalized_chunks_file"]),
    )


def embed_query(text: str, embedder: Any, config: dict[str, Any]) -> list[float]:
    # Usar el mismo modelo y normalización que los documentos; query_prefix
    # permite adaptar los modelos que distinguen consultas de pasajes.
    prefix = config.get("query_prefix", "")
    return embedder.embed_documents([prefix + text])[0]
