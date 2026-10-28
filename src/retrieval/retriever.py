# ============================================================
# RETRIEVER DEL CHATBOT
# ============================================================

"""
Este módulo se encarga de recuperar de Qdrant los chunks
más relevantes para una pregunta del usuario.

Flujo:

1. Recibe una pregunta.
2. Genera su embedding.
3. Construye el filtro de Qdrant según el tipo de documento
   y la estrategia de chunking configurada.
4. Realiza la búsqueda vectorial.
5. Devuelve los chunks recuperados junto con sus metadatos.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Any, Dict, List

import torch

from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.http import models


# ============================================================
# CREAR MODELO DE EMBEDDINGS
# ============================================================

def create_embeddings(
    embeddings_config: Dict[str, Any],
) -> HuggingFaceEmbeddings:

    """
    Carga el modelo de embeddings utilizado para transformar
    las preguntas del usuario en vectores.

    Debe ser el mismo modelo utilizado para indexar los chunks
    almacenados en Qdrant.
    """

    device = (
        "cuda"
        if torch.cuda.is_available()
        else "cpu"
    )

    print(
        f"Cargando modelo de embeddings: "
        f"{embeddings_config['model_name']} "
        f"en {device}"
    )

    embeddings = HuggingFaceEmbeddings(
        model_name=embeddings_config["model_name"],
        model_kwargs={
            "device": device,
        },
        encode_kwargs={
            "normalize_embeddings":
                embeddings_config.get(
                    "normalize_embeddings",
                    True,
                )
        },
    )

    return embeddings


# ============================================================
# CONECTAR A QDRANT
# ============================================================

def connect_to_qdrant(
    qdrant_config: Dict[str, Any],
) -> QdrantClient:

    """
    Crea la conexión con Qdrant.
    """

    client = QdrantClient(
        host=qdrant_config["host"],
        port=qdrant_config["port"],
        timeout=60,
    )

    return client


# ============================================================
# CREAR FILTRO DE RETRIEVAL
# ============================================================

def create_retrieval_filter(
    strategies: Dict[str, str],
) -> models.Filter:

    """
    Construye el filtro que determina qué chunks pueden
    participar en el retrieval.

    Ejemplo:

        paper
            -> recursive_chunking

        guideline
            -> recursive_chunking

        ficha_tecnica
            -> JSONChunking

    Cada combinación document_type + strategy constituye
    una condición válida.

    Las distintas combinaciones se unen mediante OR.
    """

    document_filters = []

    for document_type, strategy in strategies.items():

        document_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_type",
                    match=models.MatchValue(
                        value=document_type
                    ),
                ),
                models.FieldCondition(
                    key="strategy",
                    match=models.MatchValue(
                        value=strategy
                    ),
                ),
            ]
        )

        document_filters.append(
            document_filter
        )

    query_filter = models.Filter(
        should=document_filters
    )

    return query_filter


# ============================================================
# FORMATEAR CHUNKS
# ============================================================

def format_hits(
    hits,
) -> List[Dict[str, Any]]:

    """
    Convierte los resultados devueltos por Qdrant en una
    estructura sencilla para utilizar posteriormente en
    el pipeline del RAG.

    Se conserva tanto el texto como los metadatos para poder
    mostrar las fuentes utilizadas por el chatbot.
    """

    chunks = []

    for rank, hit in enumerate(
        hits,
        start=1,
    ):

        payload = hit.payload or {}

        metadata = payload.get(
            "metadata",
            {},
        )

        chunks.append(
            {
                "rank": rank,
                "score": hit.score,

                "chunk_id": payload.get(
                    "original_chunk_id"
                ),

                "document_id": payload.get(
                    "document_id"
                ),

                "document_type": payload.get(
                    "document_type"
                ),

                "strategy": payload.get(
                    "strategy"
                ),

                "chunk_index": payload.get(
                    "chunk_index"
                ),

                "group_name": metadata.get(
                    "group_name"
                ),

                "text": payload.get(
                    "text"
                ),
            }
        )

    return chunks


# ============================================================
# RECUPERAR CHUNKS
# ============================================================

def retrieve_chunks(
    question: str,
    embeddings: HuggingFaceEmbeddings,
    client: QdrantClient,
    collection_name: str,
    top_k: int,
    strategies: Dict[str, str],
) -> List[Dict[str, Any]]:

    """
    Recupera los chunks más relevantes para una pregunta.

    La búsqueda se realiza únicamente entre los chunks que
    cumplen las combinaciones document_type + strategy
    especificadas en la configuración.
    """

    # --------------------------------------------------------
    # 1. Generar embedding de la pregunta
    # --------------------------------------------------------

    query_vector = embeddings.embed_query(
        question
    )


    # --------------------------------------------------------
    # 2. Construir filtro
    # --------------------------------------------------------

    query_filter = create_retrieval_filter(
        strategies
    )


    # --------------------------------------------------------
    # 3. Buscar en Qdrant
    # --------------------------------------------------------

    search_result = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    ).points


    # --------------------------------------------------------
    # 4. Formatear resultados
    # --------------------------------------------------------

    chunks = format_hits(
        search_result
    )

    return chunks