
# ============================================================
# CONSTRUCCIÓN DEL CONTEXTO DOCUMENTAL
# ============================================================

"""
Este módulo construye el contexto documental que se
proporcionará al modelo de lenguaje durante la generación.

Responsabilidades:

1. Recibir los chunks recuperados por el retriever.
2. Extraer el texto de cada chunk.
3. Concatenar los textos en un único contexto.
4. Devolver el contexto preparado para el LLM.

Los metadatos de los chunks no se incorporan al contexto,
pero permanecen disponibles en los resultados originales
del retrieval para su posterior utilización.

Este módulo no realiza búsquedas en Qdrant, no construye
prompts y no genera respuestas.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Any, Dict, List


# ============================================================
# CONSTRUIR CONTEXTO
# ============================================================

def build_context(
    chunks: List[Dict[str, Any]],
) -> str:
    """
    Construye el contexto documental a partir de los
    chunks recuperados por el sistema RAG.

    Parameters
    ----------
    chunks:
        Lista de chunks recuperados por el retriever.

        Cada chunk debe contener un campo "text"
        con su contenido documental.

        Ejemplo:
        [
            {
                "text": "Primer fragmento documental.",
                "score": 0.95,
                "document_id": "doc_001"
            },
            {
                "text": "Segundo fragmento documental.",
                "score": 0.87,
                "document_id": "doc_002"
            }
        ]

    Returns
    -------
    str
        Contexto documental formado por los textos
        de los chunks, separados por dos saltos de línea.

        Si no hay chunks con texto, devuelve una
        cadena vacía.
    """

    # --------------------------------------------------------
    # 1. Inicializar fragmentos de contexto
    # --------------------------------------------------------

    context_parts = []

    # --------------------------------------------------------
    # 2. Extraer texto de los chunks
    # --------------------------------------------------------

    for chunk in chunks:

        text = chunk.get("text")

        if text:
            context_parts.append(text)

    # --------------------------------------------------------
    # 3. Concatenar fragmentos
    # --------------------------------------------------------

    context = "\n\n".join(context_parts)

    # --------------------------------------------------------
    # 4. Devolver contexto
    # --------------------------------------------------------

    return context
