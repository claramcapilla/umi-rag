
# ============================================================
# CONSTRUCCIÓN DE PROMPTS PARA EL RAG
# ============================================================

"""
Este módulo construye los mensajes que se envían al LLM.

Responsabilidades:

1. Definir las instrucciones del sistema.
2. Incorporar el historial conversacional.
3. Incorporar el contexto documental recuperado.
4. Añadir la pregunta actual del usuario.
5. Devolver los mensajes en el formato compatible con vLLM.

Este módulo no realiza peticiones HTTP, no recupera
documentos y no genera respuestas.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Dict, List, Optional


# ============================================================
# PROMPT DE SISTEMA
# ============================================================

SYSTEM_PROMPT = (
    "Eres un asistente especializado en responder preguntas "
    "utilizando información recuperada de una base documental. "
    "Responde utilizando únicamente la información proporcionada "
    "en el contexto. "
    "No inventes información ni utilices conocimiento externo. "
    "Si el contexto no contiene información suficiente para "
    "responder a la pregunta, indícalo claramente. "
    "Utiliza el historial de conversación para interpretar "
    "las preguntas de seguimiento, pero no lo consideres "
    "una fuente de información documental. "
    "Responde de forma clara, precisa y concisa."
)


# ============================================================
# CONSTRUIR MENSAJES
# ============================================================

def build_messages(
    question: str,
    context: str,
    history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Construye los mensajes que se enviarán al LLM.

    Parameters
    ----------
    question:
        Pregunta actual del usuario.

    context:
        Contexto documental construido a partir de los
        chunks recuperados por el sistema RAG.

    history:
        Historial anterior de la conversación.

        Debe contener mensajes con formato:
        [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ]

        El historial se utiliza para interpretar preguntas
        de seguimiento, pero no como fuente documental.

    system_prompt:
        Instrucciones del sistema personalizadas.
        Si no se proporciona, se utiliza SYSTEM_PROMPT.

    Returns
    -------
    List[Dict[str, str]]
        Lista de mensajes compatible con la API de
        chat completions de vLLM.
    """

    # --------------------------------------------------------
    # 1. Seleccionar prompt de sistema
    # --------------------------------------------------------

    selected_system_prompt = (
        SYSTEM_PROMPT
        if system_prompt is None
        else system_prompt
    )

    # --------------------------------------------------------
    # 2. Construir mensaje del usuario
    # --------------------------------------------------------

    user_prompt = (
        f"CONTEXTO:\n"
        f"{context}\n\n"
        f"PREGUNTA:\n"
        f"{question}"
    )

    # --------------------------------------------------------
    # 3. Inicializar mensajes
    # --------------------------------------------------------

    messages = [
        {
            "role": "system",
            "content": selected_system_prompt,
        }
    ]

    # --------------------------------------------------------
    # 4. Incorporar historial conversacional
    # --------------------------------------------------------

    if history:
        messages.extend(history)

    # --------------------------------------------------------
    # 5. Incorporar pregunta y contexto documental
    # --------------------------------------------------------

    messages.append(
        {
            "role": "user",
            "content": user_prompt,
        }
    )

    return messages
