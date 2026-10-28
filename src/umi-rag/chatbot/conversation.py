
# ============================================================
# GESTIÓN DEL HISTORIAL CONVERSACIONAL
# ============================================================

"""
Este módulo valida y prepara el historial conversacional
que recibe el chatbot.

Responsabilidades:

1. Comprobar que el historial tiene un formato válido.
2. Conservar únicamente mensajes de usuario y asistente.
3. Limitar la longitud de cada mensaje.
4. Limitar el número de mensajes anteriores.
5. Comprobar que la conversación comienza por el usuario
   y termina con un mensaje del usuario.
6. Separar la pregunta actual del historial anterior.

Este módulo no almacena conversaciones, no realiza
peticiones HTTP y no genera respuestas.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# VALIDAR Y LIMPIAR HISTORIAL
# ============================================================

def clean_history(
    raw: Any,
    max_history_messages: int,
    max_message_chars: int,
) -> Optional[List[Dict[str, str]]]:
    """
    Valida y limpia el historial recibido del navegador.

    Parameters
    ----------
    raw:
        Lista de mensajes recibida del navegador.

    max_history_messages:
        Número máximo de mensajes que se conservarán.

    max_message_chars:
        Número máximo de caracteres por mensaje.

    Returns
    -------
    Optional[List[Dict[str, str]]]
        Historial validado o None si la conversación
        no tiene un formato válido.
    """

    # --------------------------------------------------------
    # 1. Comprobar formato del historial
    # --------------------------------------------------------

    if not isinstance(raw, list):
        return None

    # --------------------------------------------------------
    # 2. Filtrar y limitar mensajes
    # --------------------------------------------------------

    history = [
        {
            "role": message["role"],
            "content": message["content"][:max_message_chars],
        }
        for message in raw
        if isinstance(message, dict)
        and message.get("role") in ("user", "assistant")
        and isinstance(message.get("content"), str)
    ][-max_history_messages:]

    # --------------------------------------------------------
    # 3. Asegurar que comienza por el usuario
    # --------------------------------------------------------

    while history and history[0]["role"] != "user":
        history.pop(0)

    # --------------------------------------------------------
    # 4. Comprobar que termina con una pregunta
    # --------------------------------------------------------

    if not history or history[-1]["role"] != "user":
        return None

    return history


# ============================================================
# SEPARAR PREGUNTA E HISTORIAL
# ============================================================

def split_conversation(
    history: List[Dict[str, str]],
) -> Tuple[str, List[Dict[str, str]]]:
    """
    Separa la pregunta actual del historial anterior.

    Parameters
    ----------
    history:
        Lista de mensajes previamente validada
        mediante clean_history().

    Returns
    -------
    Tuple[str, List[Dict[str, str]]]
        - Pregunta actual del usuario.
        - Mensajes anteriores de la conversación.
    """

    if not history or history[-1]["role"] != "user":
        raise ValueError(
            "La conversación debe terminar con un mensaje del usuario."
        )

    question = history[-1]["content"]

    previous_messages = history[:-1]

    return question, previous_messages
