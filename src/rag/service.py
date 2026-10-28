"""Conecta recuperación y generación para atender preguntas del chatbot."""

from __future__ import annotations

from typing import Any, Protocol


class _Retriever(Protocol):
    def retrieve(self, question: str) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class RagPipeline:
    """Pipeline reutilizable de recuperación y generación."""

    def __init__(self) -> None:
        self._retriever: _Retriever | None = None

    def answer(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        from django.conf import settings
        from generation.generator import generate_answer
        if self._retriever is None:
            from retrieval.retriever import Retriever

            self._retriever = Retriever()

        chunks = self._retriever.retrieve(question)
        generation_config = {
            "base_url": settings.VLLM_URL,
            "model": settings.VLLM_MODEL,
            "max_tokens": settings.MAX_TOKENS,
            "temperature": settings.TEMPERATURE,
        }
        answer = generate_answer(
            question=question,
            chunks=chunks,
            generation_config=generation_config,
            history=history,
        )
        return answer, chunks

    def close(self) -> None:
        if self._retriever is not None:
            self._retriever.close()
            self._retriever = None


def create_pipeline() -> RagPipeline:
    """Crea el pipeline y difiere la carga de modelos a la primera consulta."""
    return RagPipeline()


__all__ = ["RagPipeline", "create_pipeline"]
