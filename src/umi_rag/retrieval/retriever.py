"""Recupera chunks relevantes para una pregunta directa del chatbot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qdrant_client import models

from umi_rag.indexing.embedder import create_embedder
from umi_rag.indexing.pipeline import DEFAULT_CONFIG, embed_query, load_config
from umi_rag.indexing.qdrant_store import create_client

RETRIEVAL_CONFIG = DEFAULT_CONFIG.parent / "retrieval_config.json"


class Retriever:
    """Reusable retrieval client that keeps the model and Qdrant connection open."""

    def __init__(
        self,
        config_file: Path = DEFAULT_CONFIG,
        retrieval_config_file: Path = RETRIEVAL_CONFIG,
    ) -> None:
        # Cargar el modelo y abrir Qdrant una sola vez permite reutilizar esta
        # instancia entre varias preguntas de una misma conversación.
        self.config = load_config(config_file)
        with retrieval_config_file.open("r", encoding="utf-8") as stream:
            self.retrieval_config = json.load(stream)
        embedding_config = self.config["embeddings"]
        if not embedding_config.get("model_name"):
            raise ValueError(
                "Configura embeddings.model_name en indexing_config.json "
                "antes de recuperar."
            )
        self.embedder = create_embedder(embedding_config)
        self.client = create_client(self.config["qdrant"])

    def retrieve(
        self,
        question: str,
        top_k: int | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        # Retrieval recibe una pregunta individual; no lee ground truth ni
        # genera la respuesta final del chatbot.
        if not question.strip():
            raise ValueError("La pregunta no puede estar vacía.")
        limit = (
            top_k if top_k is not None else int(self.retrieval_config["top_k"])
        )
        if limit < 1:
            raise ValueError("top_k debe ser al menos 1.")
        embedding_config = self.config["embeddings"]
        # La consulta se vectoriza con el mismo modelo/prefijo que se configuró
        # para la indexación de documentos.
        vector = embed_query(question, self.embedder, embedding_config)
        conditions = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in (filters or {}).items()
        ]
        # Los filtros son opcionales y se aplican al payload de Qdrant antes
        # de devolver los vecinos más cercanos.
        points = self.client.query_points(
            collection_name=self.config["qdrant"]["collection_name"],
            query=vector,
            query_filter=models.Filter(must=conditions) if conditions else None,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        ).points
        # Devolver el texto y las referencias permite que una futura capa de
        # contexto/citas los prepare para el LLM generador.
        return [
            {
                "rank": rank,
                "score": point.score,
                "chunk_id": (point.payload or {}).get("chunk_id"),
                "document_id": (point.payload or {}).get("document_id"),
                "document_type": (point.payload or {}).get("document_type"),
                "source_file": (point.payload or {}).get("source_file"),
                "text": (point.payload or {}).get("text"),
                "metadata": (point.payload or {}).get("metadata", {}),
            }
            for rank, point in enumerate(points, start=1)
        ]

    def close(self) -> None:
        # Cerrar el cliente al terminar libera los recursos de red.
        self.client.close()

    def __enter__(self) -> Retriever:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def retrieve(
    question: str,
    top_k: int | None = None,
    config_file: Path = DEFAULT_CONFIG,
    retrieval_config_file: Path = RETRIEVAL_CONFIG,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Ayudante para una consulta; para el chatbot reutilizar Retriever."""
    with Retriever(config_file, retrieval_config_file) as retriever:
        return retriever.retrieve(question, top_k=top_k, filters=filters)
