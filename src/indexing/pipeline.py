"""Orquesta el flujo de indexación de chunks hasta Qdrant.

Carga la configuración, normaliza las entradas de chunks, genera o recupera
sus embeddings desde la caché y crea o actualiza los puntos de la colección
configurada. También ofrece una operación para normalizar sin cargar el modelo
ni conectarse a Qdrant, una entrada para indexar chunks ya preparados con el
mismo flujo y una función para generar el embedding de una consulta con el
prefijo configurado para consultas.
"""

# ============================================================
# IMPORTS
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Configuración y flujo de indexación
from indexing.embedder import create_embedder, resolve_worker_config
from indexing.embedding_cache import embed_chunks_with_cache
from indexing.normalizer import load_normalized_chunks, write_normalized_chunks
from indexing.qdrant_store import (
    create_client,
    delete_points,
    ensure_collection,
    point_id_for_chunk,
    upsert_chunks,
)

# ============================================================
# RUTAS Y CONFIGURACIÓN
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "indexing_config.json"


def load_config(config_file: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Carga la configuración JSON de indexación.

    Args:
        config_file: Ruta del archivo de configuración.

    Returns:
        La configuración como un objeto JSON.

    Raises:
        ValueError: Si el contenido JSON no es un objeto.
        OSError, json.JSONDecodeError: Si no se puede leer o decodificar el archivo.
    """
    # Todas las fases leen el mismo archivo para mantener modelo y colección
    # consistentes entre la indexación y las consultas.
    with config_file.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"La configuración debe ser un objeto JSON: {config_file}")
    return config


def _resolve(root: Path, value: str) -> Path:
    """Resuelve una ruta de configuración relativa a la raíz del proyecto.

    Las rutas absolutas se devuelven sin anteponer ``root``.
    """
    # Las rutas relativas de configuración se interpretan desde la raíz
    # del proyecto, no desde el directorio actual del proceso.
    path = Path(value)
    return path if path.is_absolute() else root / path


def _validate_batch_sizes(indexing_config: dict[str, Any]) -> None:
    for key in ("batch_size", "embedding_batch_size", "cache_batch_size"):
        if int(indexing_config[key]) < 1:
            raise ValueError(f"indexing.{key} debe ser al menos 1.")


# ============================================================
# INDEXAR CHUNKS NORMALIZADOS
# ============================================================

def run_indexing(config_file: Path = DEFAULT_CONFIG) -> int:
    """Ejecuta la normalización, generación de embeddings y escritura en Qdrant.

    Valida la configuración y los IDs de chunks, reutiliza la caché de
    embeddings cuando está habilitada y cierra el cliente Qdrant incluso si
    falla la escritura. Los errores de E/S, del proveedor de embeddings y de
    Qdrant se propagan desde la fase correspondiente.

    Args:
        config_file: Ruta del archivo de configuración; sus rutas relativas
            se resuelven desde la raíz del proyecto.

    Returns:
        Número de puntos nuevos o modificados enviados a Qdrant.

    Raises:
        ValueError: Si la configuración o los chunks no cumplen los requisitos.
    """
    root = config_file.resolve().parent.parent
    config = load_config(config_file)
    paths = config["paths"]
    indexing_config = config["indexing"]
    normalized_file = _resolve(root, paths["normalized_chunks_file"])
    _validate_batch_sizes(indexing_config)

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
    result = _index_chunks(chunks, config, root, config_file)
    updated_count = result["updated_count"]
    print(f"Chunks nuevos o modificados en Qdrant: {updated_count}/{len(chunks)}")
    return updated_count


# ============================================================
# INDEXAR CHUNKS PREPARADOS
# ============================================================

def index_chunks(
    chunks: list[dict[str, Any]],
    config_file: Path = DEFAULT_CONFIG,
    previous_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Indexa chunks en Qdrant usando la configuración común del proyecto.

    Esta operación comparte el modelo, la caché, la colección y el formato de
    payload con la indexación por lotes. Si se proporcionan ``previous_ids``,
    elimina los puntos que ya no están representados por los chunks nuevos,
    después de completar correctamente el upsert.
    """
    config_file = Path(config_file).resolve()
    config = load_config(config_file)
    root = config_file.parent.parent
    return _index_chunks(chunks, config, root, config_file, previous_ids)


# ============================================================
# FLUJO COMPARTIDO DE EMBEDDINGS Y QDRANT
# ============================================================

def _index_chunks(
    chunks: list[dict[str, Any]],
    config: dict[str, Any],
    root: Path,
    config_file: Path,
    previous_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Ejecuta las fases compartidas de embeddings y persistencia vectorial."""
    if not chunks:
        raise ValueError("No hay chunks para indexar.")

    embedding_config = config["embeddings"]
    indexing_config = config["indexing"]
    qdrant_config = config["qdrant"]
    paths = config["paths"]
    _validate_batch_sizes(indexing_config)

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
    if len(vectors) != len(chunks):
        raise ValueError("No se ha generado un embedding por chunk.")
    vector_size = len(vectors[0])
    point_ids = [point_id_for_chunk(str(chunk["chunk_id"])) for chunk in chunks]
    stale_ids = list(set(previous_ids or []) - set(point_ids))

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
        if stale_ids:
            delete_points(client, qdrant_config["collection_name"], stale_ids)
    finally:
        client.close()
    return {
        "qdrant_ids": point_ids,
        "updated_count": updated_count,
        "deleted_count": len(stale_ids),
        "embedding_model": model_name,
        "collection_name": qdrant_config["collection_name"],
    }


# ============================================================
# NORMALIZAR SIN INDEXAR
# ============================================================

def normalize_only(config_file: Path = DEFAULT_CONFIG) -> int:
    """Normaliza los chunks y escribe el archivo JSONL configurado.

    No carga el modelo de embeddings ni se conecta a Qdrant.

    Args:
        config_file: Ruta del archivo de configuración.

    Returns:
        Número de chunks escritos.
    """
    # Permite inspeccionar los registros normalizados sin cargar el modelo
    # ni requerir una conexión activa a Qdrant.
    root = config_file.resolve().parent.parent
    config = load_config(config_file)
    paths = config["paths"]
    return write_normalized_chunks(
        _resolve(root, paths["chunks_dir"]),
        _resolve(root, paths["normalized_chunks_file"]),
    )


# ============================================================
# GENERAR EMBEDDING DE CONSULTA
# ============================================================

def embed_query(text: str, embedder: Any, config: dict[str, Any]) -> list[float]:
    """Genera el embedding de una consulta con el prefijo configurado.

    ``embedder`` debe proporcionar ``embed_documents`` y usar el mismo modelo
    y normalización que se emplean para los documentos.

    Args:
        text: Texto de la consulta.
        embedder: Modelo de embeddings ya inicializado.
        config: Configuración que puede incluir ``query_prefix``.

    Returns:
        El vector de la consulta.
    """
    # Usar el mismo modelo y normalización que los documentos; query_prefix
    # permite adaptar los modelos que distinguen consultas de pasajes.
    prefix = config.get("query_prefix", "")
    return embedder.embed_documents([prefix + text])[0]
