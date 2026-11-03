"""Operaciones de persistencia vectorial para la etapa de indexación.

Este módulo crea clientes Qdrant, comprueba o prepara colecciones de vectores
densos con distancia coseno, y escribe o elimina puntos. Los IDs de los puntos
se derivan de forma determinista del ``chunk_id``; los upserts omiten los
chunks cuyo payload no ha cambiado. La colección existente nunca se reemplaza
automáticamente si no es compatible: se informa el error para que cualquier
migración o recreación sea explícita.
"""

# ============================================================
# IMPORTS
# ============================================================

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from typing import Any

from qdrant_client import QdrantClient, models
from qdrant_client.http.models import ExtendedPointId


# ============================================================
# CONECTAR Y PREPARAR LA COLECCIÓN
# ============================================================

def create_client(config: dict[str, Any]) -> QdrantClient:
    """Crea un cliente Qdrant usando la configuración y las variables de entorno.

    ``config`` debe incluir ``host`` y ``port``. Si están definidas,
    ``QDRANT_HOST`` y ``QDRANT_PORT`` tienen prioridad sobre esos valores.
    El cliente usa un timeout de 60 segundos; los errores de conexión se
    propagan desde ``QdrantClient``.
    """
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
    """Comprueba que exista una colección compatible o la crea.

    Las colecciones nuevas se configuran con un único vector denso, distancia
    coseno y la dimensión indicada. Si la colección ya existe, no se modifica:
    se valida que use un vector denso único de esa dimensión y distancia. En
    caso contrario, lanza ``ValueError``; nunca elimina ni recrea la colección.
    """
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
        if vectors.distance != models.Distance.COSINE:
            raise ValueError(
                f"La colección {collection_name} usa distancia "
                f"{vectors.distance}, pero la indexación requiere "
                f"{models.Distance.COSINE}."
            )
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=vector_size,
            distance=models.Distance.COSINE,
        ),
    )


# ============================================================
# INSERTAR Y ELIMINAR PUNTOS
# ============================================================

def upsert_chunks(
    client: QdrantClient,
    collection_name: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
    batch_size: int,
    embedding_model: str,
) -> int:
    """Inserta o actualiza en Qdrant los chunks cuyos payloads hayan cambiado.

    ``chunks`` y ``vectors`` deben tener la misma longitud y compartir orden;
    cada chunk debe incluir ``chunk_id`` y ``text``. El ID del punto se deriva
    de forma determinista de ``chunk_id``. El payload conserva los campos del
    chunk, añade ``embedding_model`` y contiene ``text``. Se omiten los puntos
    cuyo payload existente coincide, y el resto se envía en lotes de
    ``batch_size``.

    Devuelve el número de puntos enviados a Qdrant, no el total de chunks
    procesados. Lanza ``ValueError`` si las longitudes no coinciden o el tamaño
    de lote es menor que 1; los errores del cliente Qdrant se propagan.
    """
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
    point_ids: Sequence[ExtendedPointId],
) -> None:
    """Elimina de Qdrant los puntos indicados y espera a que termine la operación.

    Si ``point_ids`` está vacío, no realiza ninguna petición. Los errores del
    cliente Qdrant se propagan.
    """

    if not point_ids:
        return

    client.delete(
        collection_name=collection_name,
        points_selector=models.PointIdsList(points=list(point_ids)),
        wait=True,
    )
