"""Configuración del proveedor LLM, leída de los settings de Django.

Antes esto llamaba a `load_dotenv()` en tiempo de import —mutando el `os.environ` del worker
de Celery y dejando la API key al alcance de todo el proceso— y caía a
`https://api.deepseek.com` cuando faltaba `LLM_BASE_URL`: egress fuera del EEE por defecto y
sin aviso. Ahora todo sale de `settings`, ningún valor tiene default y el endpoint se valida
contra una allowlist explícita.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def get_llm_api_key() -> str:
    key: str = settings.LLM_API_KEY
    if not key:
        raise ImproperlyConfigured("LLM_API_KEY no está definida; sin ella no se llama al LLM.")
    return key


def get_llm_model() -> str:
    model: str = settings.LLM_JSON_MODEL
    if not model:
        raise ImproperlyConfigured("LLM_JSON_MODEL no está definido.")
    return model


def get_llm_base_url() -> str:
    """Endpoint del LLM, validado contra `LLM_ALLOWED_ENDPOINTS`.

    La allowlist existe porque lo que se envía es el texto completo del documento: un
    endpoint equivocado no es un error de red, es una transferencia a un tercero.

    El `list(...)` **sostiene la guarda, no es cosmético**. Si la setting llegara como cadena —y
    cadena es lo que se escribe en `docker-compose.yml` y en el `.env`—, `url not in allowed` sería
    una comparación de subcadena y `http://evil` quedaría autorizado por una allowlist que
    contuviera `http://evil.example`. Envuelta en `list`, una cadena se descompone en caracteres y
    la comparación falla cerrada.

    La comparación es por **igualdad exacta** y así debe seguir. Una barra final o un puerto
    explícito de más obligan a declarar el destino tal cual, que es fricción en la dirección
    segura; «arreglarla» comparando por prefijo autorizaría
    `http://localhost:11434.de-un-tercero.example`.
    """
    url: str = settings.LLM_BASE_URL
    if not url:
        raise ImproperlyConfigured("LLM_BASE_URL no está definida y no tiene valor por defecto.")
    allowed: list[str] = list(settings.LLM_ALLOWED_ENDPOINTS)
    if url not in allowed:
        raise ImproperlyConfigured(
            f"LLM_BASE_URL={url!r} no está en LLM_ALLOWED_ENDPOINTS ({allowed!r}). "
            "Añádelo explícitamente si de verdad quieres enviarle los documentos."
        )
    logger.info("LLM endpoint en uso: %s", url)
    return url


_REASONING_VALIDAS = {"", "low", "medium", "high", "disabled"}


def get_llm_reasoning_kwargs() -> dict[str, Any]:
    """Argumentos extra para controlar el razonamiento del modelo, desde `settings`.

    Un modelo capaz de razonar reparte el tope de salida entre razonamiento y contenido: con
    `max_tokens` corto la respuesta vuelve **vacía y sin error** (`finish_reason=length`, cero
    caracteres de contenido). Medido sobre `granite4.2:8b` en Ollama con `max_tokens=512`: sin
    acotar, agota los 512 tokens pensando y devuelve 0 de contenido; apagado, responde en 62.

    **Ollama enciende el razonamiento por defecto en los modelos capaces** y solo lo gobierna
    `reasoning_effort`; el `/v1` ignora en silencio las otras formas —el `think` nativo y el
    `thinking={"type": "disabled"}` estilo DeepSeek—, así que enviar esas equivale a no mandar
    nada y el modelo razona igual. `low`/`high` van como parámetro con nombre; **apagarlo del
    todo exige `reasoning_effort="none"`**, un valor propio de Ollama que el SDK rechaza como
    literal tipado, de modo que va por `extra_body`.

    Va en `settings` y no en `config.py` porque depende del proveedor y del modelo desplegado,
    igual que el endpoint y el nombre del modelo. Vacío deja decidir al proveedor.

    Ojo: la API **acepta en silencio** parámetros que no aplica, así que un valor mal escrito no
    daría error, solo dejaría de tener efecto. De ahí la validación.
    """
    valor: str = settings.LLM_REASONING
    if valor not in _REASONING_VALIDAS:
        raise ImproperlyConfigured(
            f"LLM_REASONING={valor!r} no es válido; use uno de {sorted(_REASONING_VALIDAS)}."
        )
    if valor == "disabled":
        return {"extra_body": {"reasoning_effort": "none"}}
    if valor:
        return {"reasoning_effort": valor}
    return {}


_RAZONAMIENTO_SIN_MUESTREO = {"low", "medium", "high"}
_NIVELES_DE_SERVICIO = {"", "auto", "default", "flex", "priority"}
_PARAMETROS_DE_TOPE = {"max_tokens", "max_completion_tokens"}


def get_llm_sampling_allowed() -> bool:
    return settings.LLM_REASONING not in _RAZONAMIENTO_SIN_MUESTREO


def get_llm_service_tier_kwargs() -> dict[str, Any]:
    nivel: str = settings.LLM_SERVICE_TIER
    if nivel not in _NIVELES_DE_SERVICIO:
        raise ImproperlyConfigured(
            f"LLM_SERVICE_TIER={nivel!r} no es válido; use uno de {sorted(_NIVELES_DE_SERVICIO)}."
        )
    return {"service_tier": nivel} if nivel else {}


def get_llm_tokens_param() -> str:
    parametro: str = settings.LLM_TOKENS_PARAM
    if parametro not in _PARAMETROS_DE_TOPE:
        raise ImproperlyConfigured(
            f"LLM_TOKENS_PARAM={parametro!r} no es válido; "
            f"use uno de {sorted(_PARAMETROS_DE_TOPE)}."
        )
    return parametro


def get_llm_whole_document() -> bool:
    return bool(settings.LLM_WHOLE_DOCUMENT)


def get_llm_context_tokens() -> int:
    contexto = int(settings.LLM_CONTEXT_TOKENS)
    if contexto < 0:
        raise ImproperlyConfigured(f"LLM_CONTEXT_TOKENS={contexto} no puede ser negativo.")
    return contexto


def get_llm_table_grid() -> bool:
    return bool(settings.LLM_TABLE_GRID)


def get_llm_max_parallel_workers() -> int:
    """Secciones que se piden al LLM a la vez, por documento.

    Sale de `settings` y no de `config.py` porque hay que cuadrarlo con la concurrencia del
    worker: Celery corre con `--pool=threads --concurrency=4`, así que este número se multiplica
    por 4 llamadas simultáneas al proveedor. Con la constante fija de 6 que traía el paquete eran
    24, decididas en un módulo que no sabe nada del worker que lo ejecuta.
    """
    workers: int = settings.LLM_MAX_PARALLEL_WORKERS
    if workers < 1:
        raise ImproperlyConfigured("LLM_MAX_PARALLEL_WORKERS debe ser >= 1.")
    return workers