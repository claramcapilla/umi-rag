import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel


class Chunk(BaseModel):
    """
    Representa un único chunk de texto.
    """

    text: str
    metadata: Dict[str, Any]
    chunk_id: str
    doc_id: str


class BaseChunker(ABC):
    """
    Clase base abstracta para todas las estrategias de chunking.
    """

    @abstractmethod
    def chunk(self, text: str, doc_id: str) -> List[Chunk]:
        """
        Divide un texto en una lista de objetos Chunk.
        """
        pass

    @staticmethod
    def split_oversized_chunks(
        chunks: List[Chunk],
        token_counter: Callable[[str], int],
        max_tokens: Optional[int],
        doc_id: str,
    ) -> List[Chunk]:
        """
        Red de seguridad común para chunkers sin límite de tamaño propio
        (p.ej. los basados en similitud semántica). Cualquier chunk que
        supere max_tokens se vuelve a partir por frases, respetando el
        límite, y se renumeran los chunk_id de forma secuencial.

        Si max_tokens es None o 0, no hace nada (permite desactivar la
        comprobación).
        """

        if not max_tokens:
            return chunks

        expanded: List[Chunk] = []

        for original in chunks:
            if token_counter(original.text) <= max_tokens:
                expanded.append(original)
                continue

            pieces = BaseChunker._greedy_split_by_sentences(
                original.text, token_counter, max_tokens
            )

            for piece in pieces:
                metadata = dict(original.metadata)
                metadata["token_count"] = token_counter(piece)
                metadata["character_count"] = len(piece)
                metadata["split_from_oversized_chunk"] = True
                metadata["original_chunk_id"] = original.chunk_id

                expanded.append(
                    Chunk(
                        text=piece,
                        metadata=metadata,
                        chunk_id=original.chunk_id,  # se renumera abajo
                        doc_id=original.doc_id,
                    )
                )

        # Renumeramos siempre de forma secuencial, haya habido splits o no,
        # para que chunk_id quede consistente en todo el documento.
        for index, chunk in enumerate(expanded):
            chunk.chunk_id = f"{doc_id}_chunk_{index:04d}"

        return expanded

    @staticmethod
    def _greedy_split_by_sentences(
        text: str,
        token_counter: Callable[[str], int],
        max_tokens: int,
    ) -> List[str]:
        """
        Reparte un texto en trozos de como mucho max_tokens tokens,
        agrupando frases completas de forma voraz (nunca corta a mitad de
        frase). Si una única frase ya supera max_tokens por sí sola, se
        deja tal cual como su propio trozo: es mejor un chunk algo más
        grande que trocear una frase a la mitad.
        """

        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", text)
            if sentence.strip()
        ]

        if len(sentences) <= 1:
            return [text]

        pieces: List[str] = []
        current = ""

        for sentence in sentences:
            candidate = f"{current} {sentence}".strip() if current else sentence

            if current and token_counter(candidate) > max_tokens:
                pieces.append(current)
                current = sentence
            else:
                current = candidate

        if current:
            pieces.append(current)

        return pieces