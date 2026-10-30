"""Conexión, colección densa y upserts de puntos en Qdrant."""

from __future__ import annotations

import os
import uuid
from typing import Any

from qdrant_client import QdrantClient, models


def create_client(config: dict[str, Any]) -> QdrantClient:
    # Las variables de entorno permiten desplegar el mismo código en distintos
    # entornos sin modificar la configuración versionada.
    return QdrantClient(
        host=os.getenv("QDRANT_HOST", config["host"]),
        port=int(os.getenv("QDRANT_PORT", config["port"])),
        timeout=60,
    )


def ensure_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
) -> None:
    # No borrar colecciones existentes: la creación o reemplazo de una
    # colección debe ser una decisión explícita de operación/migración.
    if client.collection_exists(collection_name):
        collection = client.get_collection(collection_name)
        vectors = collection.config.params.vectors
        if not isinstance(vectors, models.VectorParams):
            raise ValueError(
                f"La colección {collection_name} no usa un vector denso único."
            )
        if vectors.size != vector_size:
            raise ValueError(
                f"La colección {collection_name} tiene dimensión {vectors.size}, "
                f"pero el modelo configurado produce dimensión {vector_size}."
            )
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=vector_size,
            distance=models.Distance.COSINE,
        ),
    )


def upsert_chunks(
    client: QdrantClient,
    collection_name: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
    batch_size: int,
    embedding_model: str,
) -> int:
    if len(chunks) != len(vectors):
        raise ValueError("Cada chunk debe tener exactamente un vector.")
    if batch_size < 1:
        raise ValueError("indexing.batch_size debe ser al menos 1.")
    updated_count = 0
    # Consultar puntos por lotes permite ignorar los chunks que ya están
    # indexados sin cambios; así, añadir documentos no reescribe la colección.
    for start in range(0, len(chunks), batch_size):
        chunk_batch = chunks[start : start + batch_size]
        vector_batch = vectors[start : start + batch_size]
        point_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, str(chunk["chunk_id"])))
            for chunk in chunk_batch
        ]
        existing = {
            str(point.id): point
            for point in client.retrieve(
                collection_name=collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
        }
        points = []
        for chunk, vector, point_id in zip(
            chunk_batch,
            vector_batch,
            point_ids,
        ):
            payload = {key: value for key, value in chunk.items() if key != "text"}
            payload["text"] = chunk["text"]
            payload["embedding_model"] = embedding_model
            current_point = existing.get(point_id)
            if current_point is not None and current_point.payload == payload:
                continue
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            )
        if points:
            client.upsert(collection_name=collection_name, points=points)
            updated_count += len(points)
    return updated_count


def delete_points(
    client: QdrantClient,
    collection_name: str,
    point_ids: list[str],
) -> None:
    """Elimina de Qdrant los puntos indicados."""

    if not point_ids:
        return

    client.delete(
        collection_name=collection_name,
        points_selector=models.PointIdsList(points=point_ids),
        wait=True,
    )
