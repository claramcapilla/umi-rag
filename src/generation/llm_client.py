
# ============================================================
# CLIENTE DEL MODELO DE LENGUAJE (vLLM)
# ============================================================

"""
Este módulo gestiona la comunicación con el modelo de lenguaje
a través de la API de vLLM, compatible con OpenAI.

Responsabilidades:

1. Recibir los mensajes preparados para el LLM.
2. Construir la petición HTTP a vLLM.
3. Enviar los mensajes y los parámetros de generación.
4. Comprobar que la petición se ha completado correctamente.
5. Extraer y devolver el texto de la respuesta.

Este módulo no construye prompts, no recupera documentos
y no gestiona el historial conversacional.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Any, Dict, List

import requests


# ============================================================
# GENERAR RESPUESTA MEDIANTE vLLM
# ============================================================

def generate_completion(
    messages: List[Dict[str, str]],
    generation_config: Dict[str, Any],
) -> str:
    """
    Envía una conversación a vLLM y devuelve la respuesta.

    Parameters
    ----------
    messages:
        Lista de mensajes con formato compatible con la API
        de chat completions de OpenAI.

        Ejemplo:
        [
            {"role": "system", "content": "Eres un asistente."},
            {"role": "user", "content": "Hola"}
        ]

    generation_config:
        Configuración del modelo y de la API.

        Campos:
        - base_url: dirección del servidor vLLM.
        - model: identificador del modelo.
        - max_tokens: máximo de tokens generados (opcional).
        - temperature: temperatura de generación (opcional).
        - timeout: tiempo máximo de espera (opcional).
        - api_key: token de autenticación (opcional).

    Returns
    -------
    str
        Texto generado por el modelo.

    Raises
    ------
    requests.exceptions.RequestException
        Si ocurre un error de conexión, timeout o HTTP.

    ValueError
        Si la respuesta no contiene texto válido.
    """

    # --------------------------------------------------------
    # 1. Construir endpoint
    # --------------------------------------------------------

    base_url = generation_config["base_url"].rstrip("/")
    endpoint = f"{base_url}/v1/chat/completions"

    # --------------------------------------------------------
    # 2. Construir petición
    # --------------------------------------------------------

    payload = {
        "model": generation_config["model"],
        "messages": messages,
        "max_tokens": generation_config.get("max_tokens", 500),
        "temperature": generation_config.get("temperature", 0.0),
        "chat_template_kwargs": {
            "enable_thinking": generation_config.get(
                "enable_thinking", False
            )
        },
    }

    # --------------------------------------------------------
    # 3. Configurar autenticación opcional
    # --------------------------------------------------------

    headers = {}

    api_key = generation_config.get("api_key")

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # --------------------------------------------------------
    # 4. Enviar petición
    # --------------------------------------------------------

    response = requests.post(
        endpoint,
        json=payload,
        headers=headers,
        timeout=generation_config.get("timeout", 120),
    )

    response.raise_for_status()

    # --------------------------------------------------------
    # 5. Extraer respuesta
    # --------------------------------------------------------

    result = response.json()

    try:
        answer = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            "La respuesta de vLLM no contiene el formato esperado."
        ) from exc

    if not isinstance(answer, str) or not answer.strip():
        raise ValueError(
            "vLLM ha devuelto una respuesta vacía o no válida."
        )

    return answer.strip()
