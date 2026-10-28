
# ============================================================
# GENERADOR DE RESPUESTAS DEL RAG
# ============================================================

"""
Este módulo coordina la generación de respuestas del RAG.

Flujo:

1. Recibe la pregunta del usuario.
2. Recibe los chunks recuperados por el retriever.
3. Construye el contexto documental.
4. Construye los mensajes para el LLM.
5. Solicita la generación de la respuesta a vLLM.
6. Devuelve la respuesta generada.

Este módulo no realiza búsquedas en Qdrant,
no gestiona peticiones HTTP directamente
y no almacena conversaciones.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Any, Dict, List, Optional

from umi_rag.retrieval.context_builder import build_context
from umi_rag.generation.prompts import build_messages
from umi_rag.generation.llm_client import generate_completion


# ============================================================
# GENERAR RESPUESTA
# ============================================================

def generate_answer(
    question: str,
    chunks: List[Dict[str, Any]],
    generation_config: Dict[str, Any],
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Genera una respuesta utilizando el contexto documental
    recuperado y el modelo de lenguaje configurado.

    Parameters
    ----------
    question:
        Pregunta actual del usuario.

    chunks:
        Lista de chunks recuperados por el retriever.

        Cada chunk debe contener un campo "text"
        con el contenido documental.

    generation_config:
        Configuración de la generación mediante vLLM.

        Campos:
        - base_url: dirección del servidor vLLM.
        - model: identificador del modelo.
        - max_tokens: máximo de tokens generados (opcional).
        - temperature: temperatura de generación (opcional).
        - timeout: tiempo máximo de espera (opcional).
        - api_key: token de autenticación (opcional).
        - enable_thinking: activar razonamiento (opcional).

    history:
        Historial anterior de la conversación.

        Se utiliza para interpretar preguntas de
        seguimiento, pero no como fuente documental.

    Returns
    -------
    str
        Respuesta generada por el LLM o mensaje indicando
        que no se ha encontrado información relevante.
    """

    # --------------------------------------------------------
    # 1. Comprobar que existen chunks
    # --------------------------------------------------------

    if not chunks:
        return (
            "No se ha encontrado información relevante "
            "para responder a la pregunta."
        )

    # --------------------------------------------------------
    # 2. Construir contexto documental
    # --------------------------------------------------------

    context = build_context(
        chunks=chunks
    )

    # --------------------------------------------------------
    # 3. Comprobar que el contexto contiene texto
    # --------------------------------------------------------

    if not context.strip():
        return (
            "No se ha encontrado información relevante "
            "para responder a la pregunta."
        )

    # --------------------------------------------------------
    # 4. Construir mensajes para el LLM
    # --------------------------------------------------------

    messages = build_messages(
        question=question,
        context=context,
        history=history,
    )

    # --------------------------------------------------------
    # 5. Generar respuesta mediante vLLM
    # --------------------------------------------------------

    answer = generate_completion(
        messages=messages,
        generation_config=generation_config,
    )

    # --------------------------------------------------------
    # 6. Devolver respuesta
    # --------------------------------------------------------

    return answer
