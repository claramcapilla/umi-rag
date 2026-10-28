"""Generación del JSON clínico sección a sección, llamando al LLM.

Este módulo es el que decide si un documento está listo, y ahí estaba su defecto más serio: las
dos ramas de ejecución capturaban la excepción de cada sección, la registraban y **seguían**, así
que el documento volvía sin esa sección y sin ninguna marca. Aguas abajo `_ensure_required_fields`
rellenaba los campos ausentes con `null`, borrando la última evidencia, y el documento se
archivaba e indexaba como si estuviera completo. Una guía indexada sin su posología no devuelve
un error: responde que la guía no dice nada al respecto.

Ahora las secciones que fallan se acumulan y salen en el bloque `_extraction`, y **quien decide
qué hacer con un documento parcial es el llamante** (`src/pipeline/tasks.py`), no este módulo. Es
el mismo reparto que en el enriquecimiento BIFIMED: aquí se informa del hecho, no se interpreta.

La señal de fallo es **la excepción capturada**, nunca la ausencia de un campo: los esquemas
declaran muchos campos como opcionales y el LLM omitiéndolos es el caso normal, así que tomar
«campo ausente» por «sección fallida» marcaría como incompleto casi cualquier documento.
"""

from __future__ import annotations

import contextvars
import logging
import math
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, cast

import httpx
from django.utils import timezone
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from document_processor import consumo
from document_processor.chunking import SCHEMA_VERSION
from document_processor.clinical import (
    ISO_POR_IDIOMA,
    Cifra,
    admite_copia_literal,
    cifras_que_faltan,
    claves_del_json,
    detect_document_type,
    detectar_idioma,
    fusionar_salidas,
    guideline_signal_digest,
    incorporar_tablas,
    log_validation_results,
    normalizar_idioma,
    renumerar_ids,
    resolver_referencias,
    revisar_contenido,
    ruta_en_bucle,
    safe_parse_llm_json,
    sanitize_clinical_json,
    validate_clinical_json,
)
from document_processor.clinical.cifras_sin_fuente import (
    CampoSinFuente,
    RevisorDeCifras,
    Ruta,
)
from document_processor.clinical.nombres_sin_fuente import (
    RevisorDeNombres,
    quitar_nombres_sin_fuente,
)
from document_processor.config import (
    CHUNK_MAX_CHARS,
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_RETRY_BASE_DELAY,
    LLM_SEED,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    ROUTER_CLASSIFY_CHARS,
    ROUTER_MAX_TOKENS,
    ROUTER_TEMPERATURE,
)
from document_processor.extraction import FichaTecnicaNoIngerible
from document_processor.fidelity import build_fidelity_report, output_anchoring, salient_recall
from document_processor.llm_config import (
    get_llm_api_key,
    get_llm_base_url,
    get_llm_context_tokens,
    get_llm_max_parallel_workers,
    get_llm_model,
    get_llm_reasoning_kwargs,
    get_llm_sampling_allowed,
    get_llm_service_tier_kwargs,
    get_llm_tokens_param,
    get_llm_whole_document,
)
from document_processor.orchestration import (
    documento_entero,
    jerga_del_proceso,
    revision_de_sentido,
)
from document_processor.schema import ClinicalJson, DocVariant, SectionMap, SectionSpec
from document_processor.sectioning import (
    DocumentSection,
    SectionChunk,
    get_chunk_for_section,
    get_chunk_for_section_cg,
    partir_en_ventanas,
    split_document,
)
from document_processor.sections import (
    PROMPTS_DIR,
    SECTIONS_BY_VARIANT,
    VARIANTES_CON_PROMPT,
    system_prompt_path,
)
from document_processor.sections.cifras import (
    CAMPO_DE_CIFRAS,
    get_prompt_de_cifras,
)
from document_processor.sections.documento_por_apartados import (
    CAMPO_DE_APARTADOS,
    CLAVE_DOCUMENTO_POR_APARTADOS,
)
from document_processor.sections.guia_clinica import get_section_prompt_cg
from document_processor.sections.paper import get_section_prompt_paper
from document_processor.token_cost_utils import cost_for_model, count_tokens

logger = logging.getLogger(__name__)


class SalidaDegenerada(ValueError):
    def __init__(self, mensaje: str, tokens: dict[str, int]) -> None:
        super().__init__(mensaje)
        self.tokens = tokens

    def sumar(self, tokens: dict[str, int]) -> None:
        for clave, valor in tokens.items():
            self.tokens[clave] = self.tokens.get(clave, 0) + valor


class ContextoInsuficiente(SalidaDegenerada):
    pass


VENTANA_MINIMA = 2_000
FACTOR_DE_TOKENIZADOR = 1.25
MARGEN_DE_PLANTILLA = 256
SALIDA_MINIMA = 1_024
_SIN_TOPE = sys.maxsize
MAX_CIFRAS_POR_LLAMADA = 30
_CAMPOS_DE_UNA_CIFRA = ("parameter", "value", "context")
MAX_CARACTERES_EN_REGISTRO = 160
CONTEXTO_ANTES_DEL_CAMBIO = 40
_RUTA_DE_VALOR_DE_CIFRA: Ruta = (CAMPO_DE_CIFRAS, 0, "value")


_client: OpenAI | None = None
_client_lock = threading.Lock()

Enrutado = tuple[DocVariant, dict[str, int]]
CAPITULO_DE_GUIA: Enrutado = ("guia_clinica", {"input": 0, "output": 0})

_BUILDERS: dict[DocVariant, Callable[[str, str], str]] = {
    "guia_clinica": get_section_prompt_cg,
    "paper": get_section_prompt_paper,
}


def _get_client() -> OpenAI:
    """Cliente OpenAI perezoso, para que importar el módulo no exija credenciales.

    El candado no es decorativo: en modo paralelo varias secciones entran a la vez, y el
    `if _client is None` sin sincronizar construía un cliente por hilo en el primer documento
    que procesaba un worker recién arrancado.

    El `http_client` se pasa a mano solo para apagar `trust_env`. El SDK usa httpx debajo y lo
    construye con el default, que consulta `HTTP_PROXY`/`ALL_PROXY` del entorno del proceso: la
    allowlist valida la URL, no el destino de red, así que en un contenedor detrás de proxy
    corporativo el endpoint aprobado se resolvía contra el proxy y el documento completo se
    entregaba ahí. `localhost` solo se excluye si está en `NO_PROXY`, no hay bypass implícito de
    loopback. Es el mismo agujero que se cerró en `integration/embeddings.py`, en el egress del
    documento entero en vez del de los chunks.
    """
    global _client
    with _client_lock:
        if _client is None:
            _client = OpenAI(
                api_key=get_llm_api_key(),
                base_url=get_llm_base_url(),
                http_client=httpx.Client(trust_env=False),
            )
        return _client


def _es_error_de_contexto(exc: Exception) -> bool:
    texto = str(exc).lower()
    return "context_length_exceeded" in texto or "maximum context length" in texto


def _tokens_estimados(texto: str) -> int:
    return math.ceil(count_tokens(texto) * FACTOR_DE_TOKENIZADOR) + MARGEN_DE_PLANTILLA


def _salida_que_cabe(texto: str) -> int | None:
    contexto = get_llm_context_tokens()
    return contexto - _tokens_estimados(texto) if contexto else None


def _texto_de_los_mensajes(messages: list[ChatCompletionMessageParam]) -> str:
    partes: list[str] = []
    for mensaje in messages:
        contenido = mensaje.get("content")
        if isinstance(contenido, str):
            partes.append(contenido)
        elif isinstance(contenido, list):
            partes.extend(
                str(parte.get("text", "")) for parte in contenido if isinstance(parte, dict)
            )
    return "".join(partes)


def _tope_de_salida(messages: list[ChatCompletionMessageParam], max_tokens: int) -> int:
    libre = _salida_que_cabe(_texto_de_los_mensajes(messages))
    if libre is None:
        return max_tokens
    if libre < SALIDA_MINIMA:
        raise ContextoInsuficiente(
            f"el prompt solo deja {libre} tokens de salida", {"input": 0, "output": 0}
        )
    return min(max_tokens, libre)


def _es_error_de_credenciales(exc: Exception) -> bool:
    nombre = type(exc).__name__.lower()
    return any(clave in nombre for clave in ("auth", "permission"))


def _call_llm_with_retry(
    model: str,
    messages: list[ChatCompletionMessageParam],
    temperature: float = LLM_TEMPERATURE,
    max_tokens: int = LLM_MAX_TOKENS,
    extra: dict[str, Any] | None = None,
) -> ChatCompletion:
    """Llama al LLM reintentando los errores transitorios y la respuesta vacía.

    Un fallo de credenciales no mejora esperando: reintentarlo retrasa el diagnóstico y
    multiplica el gasto.

    **Una respuesta vacía también se reintenta.** Un modelo de razonamiento puede gastar su tope
    de salida pensando y devolver cero caracteres sin lanzar ningún error (ver
    `get_llm_reasoning_kwargs` y `ROUTER_MAX_TOKENS`); `ROUTER_MAX_TOKENS` holgado reduce el modo
    de fallo pero no lo elimina, y en el enrutado un vacío no falla un documento sino **todos**.
    El vacío es intermitente —el mismo modelo lo dio en una guía y no en otra—, así que se
    reintenta con la misma semilla. Agotados los intentos, la respuesta vacía se devuelve tal
    cual para que `safe_parse_llm_json` lance el error diagnóstico de siempre, sin cambiar el
    comportamiento aguas abajo (clasificador: falla el documento; sección: extracción parcial).

    `temperature` y `max_tokens` son parámetros porque el enrutado de variante fija los suyos
    (`ROUTER_TEMPERATURE`, `ROUTER_MAX_TOKENS`) sin depender de los de sección: hoy las dos
    temperaturas valen 0, pero quien suba `LLM_TEMPERATURE` para un experimento no debe arrastrar
    con ello la decisión que elige el esquema entero del documento. Los defaults dejan intacta la
    llamada de las secciones.
    """
    opciones = _opciones_de_llamada(temperature, _tope_de_salida(messages, max_tokens), extra)
    for intento in range(LLM_MAX_RETRIES):
        inicio = time.monotonic()
        try:
            respuesta: ChatCompletion = _get_client().chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                timeout=LLM_TIMEOUT_SECONDS,
                **opciones,
            )
        except Exception as exc:
            if get_llm_context_tokens() and _es_error_de_contexto(exc):
                raise ContextoInsuficiente(str(exc), {"input": 0, "output": 0}) from exc
            if intento == LLM_MAX_RETRIES - 1 or _es_error_de_credenciales(exc):
                raise
            time.sleep(_esperar_y_avisar(intento, exc))
            continue

        _registrar_uso(model, respuesta, time.monotonic() - inicio)
        if (respuesta.choices[0].message.content or "").strip():
            return respuesta
        if intento == LLM_MAX_RETRIES - 1:
            return respuesta

        time.sleep(_esperar_y_avisar(intento, None))

    raise AssertionError(  # pragma: no cover - el último intento devuelve o relanza
        "inalcanzable: el último intento devuelve o relanza"
    )


def _opciones_de_llamada(
    temperature: float, max_tokens: int, extra: dict[str, Any] | None
) -> dict[str, Any]:
    opciones: dict[str, Any] = {get_llm_tokens_param(): max_tokens}
    if get_llm_sampling_allowed():
        opciones["temperature"] = temperature
        opciones["seed"] = LLM_SEED
    opciones.update(get_llm_service_tier_kwargs())
    opciones.update(_combinar_extra(get_llm_reasoning_kwargs(), extra or {}))
    return opciones


def _combinar_extra(razonamiento: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    combinado = {**razonamiento, **{k: v for k, v in extra.items() if k != "extra_body"}}
    cuerpo = {**razonamiento.get("extra_body", {}), **extra.get("extra_body", {})}
    if cuerpo:
        combinado["extra_body"] = cuerpo
    return combinado


def _registrar_uso(model: str, respuesta: ChatCompletion, segundos: float) -> None:
    uso = respuesta.usage
    if uso is None:
        return
    entrada = uso.prompt_tokens_details
    salida = uso.completion_tokens_details
    en_cache = (entrada.cached_tokens or 0) if entrada else 0
    razonamiento = (salida.reasoning_tokens or 0) if salida else 0
    consumo.registrar(
        model,
        respuesta.service_tier,
        uso.prompt_tokens,
        en_cache,
        uso.completion_tokens,
        razonamiento,
    )
    logger.info(
        "Uso del LLM: model=%s prompt=%d cached=%d completion=%d reasoning=%d tier=%s "
        "segundos=%.1f salida_por_segundo=%.1f",
        model,
        uso.prompt_tokens,
        en_cache,
        uso.completion_tokens,
        razonamiento,
        respuesta.service_tier,
        segundos,
        uso.completion_tokens / segundos if segundos > 0 else 0.0,
    )


def _esperar_y_avisar(intento: int, exc: Exception | None) -> float:
    """Registra el reintento y devuelve el backoff en segundos, que crece con cada intento.

    Separa las dos causas —error transitorio y respuesta vacía— en dos mensajes distintos, porque
    diagnosticar una tanda con muchos reintentos exige saber cuál fue: la vacía apunta al tope de
    razonamiento, el error al proveedor.
    """
    espera: float = LLM_RETRY_BASE_DELAY * (2**intento)
    if exc is None:
        logger.warning(
            "Respuesta vacía del LLM (intento %d/%d). Reintento en %ds",
            intento + 1,
            LLM_MAX_RETRIES,
            espera,
        )
    else:
        logger.warning(
            "Llamada al LLM fallida (intento %d/%d): %s. Reintento en %ds",
            intento + 1,
            LLM_MAX_RETRIES,
            exc,
            espera,
        )
    return espera


def generate_section_json(
    md_text: str,
    section_key: str,
    *,
    system_prompt_text: str,
    model: str | None = None,
    prompt_builder: Callable[[str, str], str] = get_section_prompt_paper,
    idioma: str = "",
    tablas_en_rejilla: str = "",
) -> dict[str, Any]:
    """Genera una sección del JSON clínico a partir de su chunk.

    `system_prompt_text` es **obligatorio y no puede estar vacío**. Ahí viven las únicas reglas
    de contención que hay, incluido el ámbito de los documentos, y esta
    función es pública: con un default a `None` bastaba con no pasarlo para llamar al LLM sin
    ninguna contención, en silencio. Con una regla innegociable el default correcto es fallar.

    Va por palabra clave a partir de `section_key` porque el orden de los parámetros cambió al
    hacerlo obligatorio: un llamante con el orden anterior, `(md, key, "modelo")`, pasaba el
    nombre del modelo **como system prompt completo** —no vacío, así que la guarda de abajo no
    salta— y volvía a llamar al LLM sin contención. Por palabra clave eso es un `TypeError`.

    Args:
        md_text: Chunk de Markdown relevante para esta sección.
        section_key: Clave de la sección (ej: "metadata").
        system_prompt_text: System prompt ya cargado, para no releer disco por sección.
        model: Modelo LLM a usar.
        prompt_builder: Constructor del prompt de sección según la variante del documento.
        idioma: Idioma del documento, para decírselo al modelo en vez de que lo infiera. La
            regla lleva en los tres system prompts desde siempre y aun así se incumplía: la
            guía inglesa de la BSACI devolvió sus 30 recomendaciones en español. Va **después**
            del extracto delimitado, que es donde la guía de contexto largo sitúa las
            instrucciones.
    """
    if not system_prompt_text.strip():
        raise ValueError("El system prompt está vacío; no se llama al LLM sin contención.")

    if model is None:
        model = get_llm_model()

    sin_rejilla = False
    if get_llm_whole_document():
        sistema = _sistema_de_documento_entero(system_prompt_text)
        tarea = _con_idioma(
            documento_entero.tarea_sin_extracto(prompt_builder(section_key, "")), idioma
        )
        documento, sin_rejilla = _documento_que_cabe(sistema, md_text, tablas_en_rejilla, tarea)
        messages = cast(
            "list[ChatCompletionMessageParam]",
            documento_entero.mensajes(sistema, documento, tarea, model),
        )
        extra = documento_entero.parametros_de_cache(model, documento)
        texto_de_entrada = sistema + documento + tarea
    else:
        section_prompt = _con_idioma(prompt_builder(section_key, md_text), idioma)
        messages = [
            {"role": "system", "content": system_prompt_text},
            {"role": "user", "content": section_prompt},
        ]
        extra = None
        texto_de_entrada = system_prompt_text + section_prompt

    respuesta = _call_llm_with_retry(model, messages, extra=extra)
    content = respuesta.choices[0].message.content or ""
    tokens = {"input": count_tokens(texto_de_entrada), "output": count_tokens(content)}
    if respuesta.choices[0].finish_reason == "length":
        raise SalidaDegenerada(f"{section_key}: la salida llegó al tope de tokens", tokens)

    parsed: dict[str, Any] = safe_parse_llm_json(content, section_key)
    if ruta := ruta_en_bucle(parsed):
        raise SalidaDegenerada(f"{section_key}: la lista {ruta} repite un mismo elemento", tokens)
    parsed["_tokens"] = tokens
    if sin_rejilla:
        parsed["_sin_rejilla"] = True
    return parsed


def _documento_que_cabe(sistema: str, md_text: str, rejilla: str, tarea: str) -> tuple[str, bool]:
    documento = documento_entero.documento_con_bloques(md_text, rejilla)
    libre = _salida_que_cabe(sistema + documento + tarea)
    if not rejilla.strip() or libre is None or libre >= LLM_MAX_TOKENS:
        return documento, False
    logger.warning("La rejilla de tablas no cabe en el contexto: la llamada va sin ella")
    return documento_entero.documento_con_bloques(md_text, ""), True


def _sistema_de_documento_entero(system_prompt_text: str) -> str:
    return f"{system_prompt_text}\n\n{documento_entero.REGLAS_DE_FIDELIDAD}"


def sistema_y_secciones(variante: DocVariant) -> tuple[str, int]:
    resuelta = _resolver_variante(variante, None)
    return _sistema_de_documento_entero(resuelta.system_prompt), len(resuelta.sections)


def _con_idioma(prompt: str, idioma: str) -> str:
    if not idioma:
        return prompt
    return (
        f"{prompt}\n\nIDIOMA DEL DOCUMENTO: {idioma}. "
        f"Los valores del JSON deben ir en {idioma}, no en otro idioma."
    )


@dataclass(frozen=True)
class _Variante:
    """Todo lo que depende del tipo de documento, resuelto una sola vez."""

    variante: DocVariant
    sections: SectionMap
    builder: Callable[[str, str], str]
    system_prompt: str


_CLASIFICADOR = "clasificador_variante"
_PROMPT_CLASIFICADOR = f"{_CLASIFICADOR}.txt"

_ETIQUETAS_ENRUTABLES: set[DocVariant] = {"guia_clinica", "paper"}


def _confianza(datos: ClinicalJson) -> float | None:
    """La confianza del clasificador, solo si es un número.

    Lo que devuelve el modelo en esa clave es texto arbitrario, y el log va a INFO —el nivel por
    defecto— en una llamada que lleva 4.000 caracteres del documento. Sin esta coacción, un
    `{"confianza": "Juan Pérez, NHC 1234567"}` pasa la validación de `variante` y se emite
    verbatim a un destino cuya retención no sigue el ciclo de vida del documento.
    """
    try:
        return float(datos.get("confianza"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _clasificar_paper_o_guia(md_text: str, model: str) -> tuple[DocVariant, dict[str, int]]:
    """Decide entre paper y guía con el LLM, porque el regex no lleva señal para distinguirlas.

    Una etiqueta que no reconozcamos es un error, no un `paper`: caer a la variante por defecto
    es exactamente el fallo que este enrutado viene a arreglar. `ficha_tecnica` tampoco vale como
    respuesta, porque esa rama la decide el regex antes de llegar aquí.

    Al extracto de apertura se le añade `guideline_signal_digest`, que cuenta las marcas de guía
    sobre el documento ENTERO. El clasificador solo ve `ROUTER_CLASSIFY_CHARS` de apertura, y una
    guía cuyas recomendaciones y niveles de evidencia viven en el cuerpo puede parecer un artículo
    en la portada. El resumen es una pista, no un voto: el regex por sí solo penaliza a las guías
    publicadas en revista por su estructura de artículo (medido: recall de guía 0.33), así que
    quien decide sigue siendo el LLM, ahora con la evidencia de todo el texto delante.
    """
    system = (PROMPTS_DIR / _PROMPT_CLASIFICADOR).read_text(encoding="utf-8")
    if not system.strip():
        raise ValueError(
            f"El prompt {_PROMPT_CLASIFICADOR} está vacío; no se llama al LLM sin contención."
        )
    extracto = f"{md_text[:ROUTER_CLASSIFY_CHARS]}\n\n{guideline_signal_digest(md_text)}"
    respuesta = _call_llm_with_retry(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": extracto},
        ],
        temperature=ROUTER_TEMPERATURE,
        max_tokens=ROUTER_MAX_TOKENS,
    )
    contenido = respuesta.choices[0].message.content or ""
    tokens = {"input": count_tokens(system + extracto), "output": count_tokens(contenido)}
    datos = safe_parse_llm_json(contenido, _CLASIFICADOR)
    etiqueta = str(datos.get("variante", "")).strip().lower()
    if etiqueta not in _ETIQUETAS_ENRUTABLES:
        raise ValueError(f"Etiqueta de variante desconocida del clasificador: {etiqueta[:40]!r}")

    logger.info("Variante enrutada por LLM: %s (confianza %s)", etiqueta, _confianza(datos))
    return cast("DocVariant", etiqueta), tokens


def resolver_doc_type(md_text: str, model: str) -> tuple[DocVariant, dict[str, int]]:
    """Enruta el documento entre paper y guía, y rechaza la ficha técnica.

    La ficha se sigue detectando con el regex, que está limpiamente separada y no merece gastar una
    llamada, pero ya no se extrae: su fuente es CIMA, que la sirve segmentada por su numeración
    normativa. Rechazarla **aquí** es lo que hace que no haya un segundo camino que se olvide de
    hacerlo, y además es gratis: ocurre antes de la única llamada de enrutado.

    Devuelve también los tokens que costó enrutar, para que el `_usage` del documento archivado
    refleje **las dos** cargas que salieron al LLM. Sin eso, los artefactos archivados no
    permiten reconstruir qué se envió, que es justo lo que sostiene la procedencia.
    """
    rechazar_ficha_tecnica(md_text)
    return _clasificar_paper_o_guia(md_text, model)


def rechazar_ficha_tecnica(md_text: str) -> None:
    if detect_document_type(md_text) == "ficha_tecnica":
        raise FichaTecnicaNoIngerible(
            "El documento es una ficha técnica: se ingiere desde CIMA por número de registro, "
            "no desde el PDF"
        )


def _resolver_variante(variante: DocVariant, ruta_prompt: str | Path | None) -> _Variante:
    """Resuelve mapa de secciones, constructor de prompt y system prompt de la variante.

    Una ruta de prompt arbitraria se rechaza: el parámetro existe para pruebas y para forzar una
    variante, no para saltarse los tres prompts revisados, que son los que llevan las reglas de
    contención y el ámbito de los documentos. Y un fichero vacío también se rechaza:
    `system_prompt_path` comprueba que exista, no que diga algo, y una cadena vacía dejaba la
    llamada sin contención.

    El `resolve()` se aplica a las dos ramas. Comparar una ruta sin resolver contra
    `PROMPTS_DIR.resolve()` funciona mientras `prompts/` sea un directorio real, pero si pasa a
    ser un symlink o un bind-mount el que rompe es el **camino normal**, en todos los documentos.

    La ficha técnica se rechaza **aquí también**, y no solo en el enrutado: pasar `ruta_prompt`
    saltaba esa comprobación y llegaba a `_BUILDERS`, que ya no tiene esa variante, con lo que
    salía un `KeyError` desnudo mientras el enrutado afirmaba que no había un segundo camino.

    El predicado es `VARIANTES_CON_PROMPT`, que es la lista **declarada** y que mypy comprueba, no
    la pertenencia a un dict privado. Con `_BUILDERS` la guarda cubría de más: una cuarta variante
    a la que se le olvidara el constructor saldría rechazada con un mensaje que afirma que es un
    SmPC y, porque `_reenqueue` niega el reintento por el nombre de esta excepción, quedaría además
    no reintentable para siempre. Un `KeyError` en `_BUILDERS` es preferible ahí: es ruidoso y
    nombra la causa real.
    """
    if variante not in VARIANTES_CON_PROMPT:
        raise FichaTecnicaNoIngerible(
            f"La variante {variante!r} no declara system prompt, así que no se extrae con el LLM. "
            "La ficha técnica se ingiere desde CIMA por número de registro."
        )

    ruta = (system_prompt_path(variante) if ruta_prompt is None else Path(ruta_prompt)).resolve()

    if not ruta.is_relative_to(PROMPTS_DIR.resolve()):
        raise ValueError(f"El system prompt debe vivir en {PROMPTS_DIR}, no en {ruta}")

    texto = ruta.read_text(encoding="utf-8")
    if not texto.strip():
        raise ValueError(f"El system prompt {ruta} está vacío; no se llama al LLM sin contención.")

    return _Variante(
        variante=variante,
        sections=SECTIONS_BY_VARIANT[variante],
        builder=_BUILDERS[variante],
        system_prompt=texto,
    )


def _chunk_de_seccion(
    variante: _Variante,
    section_key: str,
    section_info: SectionSpec,
    doc_sections: list[DocumentSection] | None,
    md_text: str,
) -> SectionChunk:
    if doc_sections is None or get_llm_whole_document():
        return SectionChunk.whole(md_text)
    if variante.variante == "guia_clinica":
        return get_chunk_for_section_cg(section_key, doc_sections, md_text, _SIN_TOPE)
    return get_chunk_for_section(section_key, doc_sections, md_text, _SIN_TOPE)


def _sumar_salida(destino: dict[str, Any], origen: dict[str, Any]) -> None:
    tokens = destino.setdefault("_tokens", {"input": 0, "output": 0})
    for clave, valor in origen.pop("_tokens", {}).items():
        tokens[clave] = tokens.get(clave, 0) + valor
    fusionar_salidas(destino, origen)


def _generar_ventana(ventana: str, generar: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    try:
        return generar(ventana)
    except SalidaDegenerada as exc:
        if len(ventana) < 2 * VENTANA_MINIMA:
            raise
        logger.warning("%s; la ventana de %s caracteres se parte en dos", exc, f"{len(ventana):,}")
        return _contando(
            exc, lambda: _generar_por_ventanas(ventana, generar, len(ventana) // 2 + 1)
        )


def _contando(exc: SalidaDegenerada, generar: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        resultado = generar()
    except SalidaDegenerada as interna:
        interna.sumar(exc.tokens)
        raise
    _sumar_salida(resultado, {"_tokens": dict(exc.tokens)})
    return resultado


def _generar_por_ventanas(
    texto: str, generar: Callable[[str], dict[str, Any]], max_chars: int = CHUNK_MAX_CHARS
) -> dict[str, Any]:
    resultado: dict[str, Any] = {}
    for ventana in partir_en_ventanas(texto, max_chars):
        _sumando_a(resultado, partial(_generar_ventana, ventana, generar))
    return resultado


def _sumando_a(resultado: dict[str, Any], generar: Callable[[], dict[str, Any]]) -> None:
    try:
        salida = generar()
    except SalidaDegenerada as exc:
        exc.sumar(resultado.get("_tokens", {}))
        raise
    _sumar_salida(resultado, salida)


def _generar_tramo(
    ventana: str, desde: int, hasta: int, generar: Callable[[str, str], dict[str, Any]]
) -> dict[str, Any]:
    try:
        datos = generar(ventana, documento_entero.tarea_de_tramo(desde, hasta))
    except ContextoInsuficiente:
        raise
    except SalidaDegenerada as exc:
        if hasta <= desde:
            raise
        mitad = (desde + hasta) // 2
        logger.warning("%s; el tramo p%d-p%d se parte en dos", exc, desde, hasta)
        return _contando(exc, lambda: _dos_tramos(ventana, desde, mitad, hasta, generar))
    apartados = datos.get(CAMPO_DE_APARTADOS)
    if isinstance(apartados, list):
        datos[CAMPO_DE_APARTADOS] = documento_entero.apartados_del_tramo(apartados, desde, hasta)
        if fuera := len(apartados) - len(datos[CAMPO_DE_APARTADOS]):
            logger.warning("Tramo p%d-p%d: %d apartados fuera de su rango", desde, hasta, fuera)
    return datos


def _dos_tramos(
    ventana: str, desde: int, mitad: int, hasta: int, generar: Callable[[str, str], dict[str, Any]]
) -> dict[str, Any]:
    resultado = _generar_tramo(ventana, desde, mitad, generar)
    _sumando_a(resultado, partial(_generar_tramo, ventana, mitad + 1, hasta, generar))
    return resultado


def _generar_apartados(texto: str, generar: Callable[[str, str], dict[str, Any]]) -> dict[str, Any]:
    resultado: dict[str, Any] = {}
    for ventana in partir_en_ventanas(texto, CHUNK_MAX_CHARS):
        tramos = documento_entero.tramos(ventana)
        if not tramos:
            _sumando_a(resultado, partial(_generar_ventana, ventana, lambda v: generar(v, "")))
        for desde, hasta in tramos:
            _sumando_a(resultado, partial(_generar_tramo, ventana, desde, hasta, generar))
    if isinstance(resultado.get(CAMPO_DE_APARTADOS), list):
        resultado[CAMPO_DE_APARTADOS] = documento_entero.unir_apartados_contiguos(
            resultado[CAMPO_DE_APARTADOS]
        )
    return resultado


@dataclass
class _Acumulador:
    """Estado que las secciones alimentan. Se toca solo desde el hilo principal.

    Con `as_completed` la recogida de resultados ocurre en el hilo que orquesta, no en los del
    pool, así que esto no necesita candado. Si algún día se recoge dentro de los workers, sí.
    """

    clinical_json: ClinicalJson = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    fallidas: dict[str, str] = field(default_factory=dict)
    sin_rejilla: set[str] = field(default_factory=set)

    def sumar_tokens(self, tokens: dict[str, int]) -> None:
        self.input_tokens += tokens.get("input", 0)
        self.output_tokens += tokens.get("output", 0)

    def acumular(self, nombre: str, datos: dict[str, Any], chunk_chars: int, total: int) -> None:
        tokens = datos.pop("_tokens", {})
        if datos.pop("_sin_rejilla", False):
            self.sin_rejilla.add(nombre)
        self.sumar_tokens(tokens)
        self.clinical_json.update(datos)
        logger.info(
            "%s: [chunk %s/%s chars] OK (%s in / %s out)",
            nombre,
            f"{chunk_chars:,}",
            f"{total:,}",
            f"{tokens.get('input', 0):,}",
            f"{tokens.get('output', 0):,}",
        )

    def registrar_fallo(self, nombre: str, exc: Exception) -> None:
        """Guarda el **tipo** de la excepción, no su texto.

        El texto va al log. Dentro del JSON clínico solo entra el tipo: ese fichero se archiva y
        se descarga desde el dashboard, y el mensaje de un error de red o del proveedor arrastra
        endpoints y detalles internos que no tienen por qué viajar dentro del documento.
        """
        self.fallidas[nombre] = type(exc).__name__
        if isinstance(exc, SalidaDegenerada):
            self.sumar_tokens(exc.tokens)
        logger.error("%s: la sección no se pudo generar: %s", nombre, exc)


def _solo_campos_declarados(
    nombre: str, datos: dict[str, Any], fields: list[str]
) -> dict[str, Any]:
    """Descarta de la salida de una sección las claves que su esquema no declara.

    El extracto del documento entra al prompt sin delimitar, así que la salida del LLM es un canal
    que el propio documento influye. Fusionarla tal cual dejaba que el modelo escribiera claves de
    control en el mismo espacio de nombres que los campos clínicos, `_extraction` incluida: la
    señal con la que el llamante decide si el documento se indexa. Eso reproducía el fallo que
    esta capa existe para cerrar —un resultado inventado indistinguible de uno legítimo— con el
    documento eligiendo de qué lado caer.

    El esquema es la autoridad de qué claves existen: `build_fragments` solo renderiza las que
    declara, así que descartar el resto no le quita nada a ningún consumidor. Lo descartado se
    registra, porque un campo real que el esquema no declare es un desajuste de esquema y tiene
    que verse en el log en vez de desaparecer.
    """
    permitidas = set(fields) | {"_tokens", "_sin_rejilla"}
    descartadas = sorted(k for k in datos if k not in permitidas)
    if descartadas:
        logger.warning(
            "%s: claves fuera del esquema descartadas: %s", nombre, ", ".join(descartadas)
        )
    return {k: v for k, v in datos.items() if k in permitidas}


_Resultado = tuple[str, dict[str, Any], int]


def _ejecutar_secciones(
    sections: SectionMap,
    generar: Callable[[str, SectionSpec], _Resultado],
    recoger: Callable[[_Resultado], None],
    registrar_fallo: Callable[[str, Exception], None],
    *,
    parallel: bool,
) -> None:
    """Recorre las secciones en paralelo o en serie, con el mismo tratamiento del fallo.

    Las dos ramas estaban duplicadas —la serial reimplementaba a mano las treinta líneas de
    acumulación de la paralela—, y con dos copias del mismo `except` era fácil arreglar el
    sumidero en una y dejarlo abierto en la otra. Aquí hay un solo camino de recogida y un solo
    sitio donde se registra que una sección no salió.

    Solo `generar` va dentro del `try`. Si `recoger` también entrara, un fallo en la acumulación
    o en el `on_section_done` del llamante se anotaría como sección fallida **después** de haber
    fusionado ya sus campos: el documento acabaría con un `_extraction` acusando a una sección
    que sí funcionó, y el llamante lo descartaría por un motivo falso.
    """

    def generar_y_recoger(key: str, info: SectionSpec) -> None:
        try:
            resultado = generar(key, info)
        except Exception as exc:
            registrar_fallo(info["name"], exc)
            return
        recoger(resultado)

    if parallel and len(sections) > 1:
        pendientes = dict(sections)
        if get_llm_whole_document():
            primera = next(iter(pendientes))
            generar_y_recoger(primera, pendientes.pop(primera))
        workers = min(len(pendientes), get_llm_max_parallel_workers())
        logger.info("Modo paralelo: %d secciones, %d workers", len(pendientes), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futuros = {
                pool.submit(contextvars.copy_context().run, generar, key, info): key
                for key, info in pendientes.items()
            }
            for futuro in as_completed(futuros):
                try:
                    resultado = futuro.result()
                except Exception as exc:
                    registrar_fallo(pendientes[futuros[futuro]]["name"], exc)
                    continue
                recoger(resultado)
        return

    for key, info in sections.items():
        logger.info("Generando: %s...", info["name"])
        generar_y_recoger(key, info)


def _ventanas_con_inicio(texto: str) -> list[tuple[int, str]]:
    inicio = 0
    ventanas = []
    for ventana in partir_en_ventanas(texto, CHUNK_MAX_CHARS):
        ventanas.append((inicio, ventana))
        inicio += len(ventana)
    return ventanas


def _pedidas(entradas: Any, cifras: list[Cifra]) -> list[dict[str, Any]]:
    claves = {clave for cifra in cifras for clave in cifra.claves}
    return [
        {campo: e[campo] for campo in _CAMPOS_DE_UNA_CIFRA if campo in e}
        for e in (entradas if isinstance(entradas, list) else [])
        if isinstance(e, dict) and claves_del_json(e.get("value")) & claves
    ]


def _descripcion(cifra: Cifra) -> str:
    return f"{cifra.literal} — en: {cifra.frase}"


def _generador_de_cifras(
    variante: _Variante, model: str, idioma: str, tablas_en_rejilla: str = ""
) -> Callable[[str, list[Cifra]], dict[str, Any]]:
    def generar(ventana: str, cifras: list[Cifra]) -> dict[str, Any]:
        descripciones = [_descripcion(c) for c in cifras]
        return generate_section_json(
            md_text=ventana,
            section_key=CAMPO_DE_CIFRAS,
            model=model,
            system_prompt_text=variante.system_prompt,
            prompt_builder=lambda _, texto: get_prompt_de_cifras(texto, descripciones),
            idioma=idioma,
            tablas_en_rejilla=tablas_en_rejilla,
        )

    return generar


def _pedir_cifras(
    acumulador: _Acumulador,
    ventana: str,
    lote: list[Cifra],
    generar: Callable[[str, list[Cifra]], dict[str, Any]],
) -> bool:
    try:
        salida = generar(ventana, lote)
    except Exception as exc:  # noqa: BLE001 - el llamante decide si hay respaldo o se registra
        logger.warning("Segunda pasada de cifras fallida (%s)", type(exc).__name__)
        if isinstance(exc, SalidaDegenerada):
            acumulador.sumar_tokens(exc.tokens)
        return False
    acumulador.sumar_tokens(salida.get("_tokens", {}))
    if salida.get("_sin_rejilla"):
        acumulador.sin_rejilla.add(CAMPO_DE_CIFRAS)
    if nuevas := _pedidas(salida.get(CAMPO_DE_CIFRAS), lote):
        fusionar_salidas(acumulador.clinical_json, {CAMPO_DE_CIFRAS: nuevas})
    return True


def _completar_cifras(
    acumulador: _Acumulador,
    md_text: str,
    generar: Callable[[str, list[Cifra]], dict[str, Any]],
) -> bool:
    datos = acumulador.clinical_json
    faltan = cifras_que_faltan(md_text, datos)
    completas = True
    for inicio, ventana in _ventanas_con_inicio(md_text):
        propias = [c for c in faltan if inicio <= c.posicion < inicio + len(ventana)]
        for desde in range(0, len(propias), MAX_CIFRAS_POR_LLAMADA):
            lote = propias[desde : desde + MAX_CIFRAS_POR_LLAMADA]
            completas = _pedir_cifras(acumulador, ventana, lote, generar) and completas
    return completas


def _sin_fuente_a_null(texto: str, revisor: RevisorDeCifras) -> str | None:
    return None if revisor.sin_fuente(texto) else texto


def _copia(cifra: Cifra, revisor: RevisorDeCifras) -> dict[str, Any] | None:
    valor = _sin_fuente_a_null(cifra.literal, revisor)
    contexto = _sin_fuente_a_null(cifra.frase, revisor)
    if valor is None and contexto is None:
        return None
    return {"parameter": None, "value": valor, "context": contexto}


def _copiar_cifras_que_faltan(
    datos: ClinicalJson, md_text: str, variante: DocVariant, revisor: RevisorDeCifras
) -> None:
    restantes = cifras_que_faltan(md_text, datos)
    if not restantes:
        return
    if not admite_copia_literal(datos, variante):
        logger.warning("%d cifras del texto faltan en el JSON; no se copian", len(restantes))
        return
    logger.info("%d cifras del texto que faltan en el JSON se copian", len(restantes))
    copiadas = [copia for c in restantes if (copia := _copia(c, revisor))]
    fusionar_salidas(datos, {CAMPO_DE_CIFRAS: copiadas})


def _contenedor(datos: Any, ruta: Ruta) -> Any:
    for paso in ruta[:-1]:
        datos = datos[paso]
    return datos


def _es_valor_de_cifra(ruta: Ruta) -> bool:
    return len(ruta) == len(_RUTA_DE_VALOR_DE_CIFRA) and (ruta[0], ruta[2]) == (
        _RUTA_DE_VALOR_DE_CIFRA[0],
        _RUTA_DE_VALOR_DE_CIFRA[2],
    )


def _recorte(texto: str | None) -> str | None:
    return texto[:MAX_CARACTERES_EN_REGISTRO] if texto else texto


def _reparar_campo(
    datos: ClinicalJson, revisor: RevisorDeCifras, campo: CampoSinFuente
) -> dict[str, Any]:
    contenedor = _contenedor(datos, campo.ruta)
    valor: str = contenedor[campo.ruta[-1]]
    sin_esas_frases = revisor.sin_frases_sin_fuente(valor)
    contenedor[campo.ruta[-1]] = sin_esas_frases
    entrada_vacia = sin_esas_frases is None and _es_valor_de_cifra(campo.ruta)
    accion = "entry_removed" if entrada_vacia else "sentences_removed"
    return {
        "field": ".".join(str(paso) for paso in campo.ruta),
        "numbers": list(campo.cifras),
        "action": accion,
        "before": _recorte(valor),
        "after": _recorte(sin_esas_frases),
    }


def _reparar_cifras(datos: ClinicalJson, revisor: RevisorDeCifras) -> list[dict[str, Any]]:
    campos = revisor.campos(datos)
    registro = [_reparar_campo(datos, revisor, c) for c in campos]
    vacias = {
        c.ruta[1] for c, r in zip(campos, registro, strict=True) if r["action"] == "entry_removed"
    }
    for indice in sorted(vacias, key=int, reverse=True):
        del datos[CAMPO_DE_CIFRAS][indice]
    if registro:
        acciones = Counter(r["action"] for r in registro)
        logger.warning("Cifras sin fuente en %d campos: %s", len(registro), dict(acciones))
    return registro


def _cuadrar_cifras(
    acumulador: _Acumulador,
    md_text: str,
    variante: _Variante,
    model: str,
    idioma: str,
    fidelity: dict[str, Any] | None,
) -> None:
    _completar_cifras(acumulador, md_text, _generador_de_cifras(variante, model, idioma))
    revisor = RevisorDeCifras(md_text)
    reparadas = _reparar_cifras(acumulador.clinical_json, revisor)
    _copiar_cifras_que_faltan(acumulador.clinical_json, md_text, variante.variante, revisor)
    if fidelity is not None and reparadas:
        fidelity["unsourced_numbers"] = reparadas


def _cuadrar_nombres(
    datos: ClinicalJson, md_text: str, variante: DocVariant, fidelity: dict[str, Any] | None
) -> None:
    hallados = quitar_nombres_sin_fuente(datos, variante, RevisorDeNombres(md_text))
    if not hallados:
        return
    acciones = Counter(h.campo.accion.value for h in hallados)
    logger.warning("Nombres sin fuente: %s", dict(acciones))
    if fidelity is not None:
        fidelity["unsourced_names"] = [
            {"field": h.campo.nombre, "name": _recorte(h.nombre), "action": h.campo.accion.value}
            for h in hallados
        ]


def generate_clinical_json_with_llm(
    md_text: str,
    model: str | None = None,
    system_prompt_path: str | Path | None = None,
    use_chunking: bool = True,
    parallel: bool = True,
    on_section_done: Callable[[str, int, int], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    tablas_en_rejilla: str = "",
    enrutado: Enrutado | None = None,
) -> ClinicalJson:
    """Genera el JSON clínico completo, sección a sección.

    Detecta la variante del documento (ficha técnica, guía clínica o artículo) y usa su mapa de
    secciones, su system prompt y su troceado.

    Si alguna sección no se puede generar, el resultado lleva un bloque `_extraction` con las que
    fallaron: el documento **no** se da por completo en silencio. Decidir qué hacer con él es del
    llamante.

    Los chunks se construyen todos antes de lanzar ninguna llamada, en el hilo que orquesta: así
    el bloque `_fidelity` (capas 0 y 1 del revisor) mide la cobertura de entrada de **todas** las
    secciones, también de las que luego fallen en el LLM, y el acumulador sigue sin necesitar
    candado. Con `use_chunking=False` no hay troceado que medir y el bloque no se emite.

    Args:
        md_text: Markdown limpio del documento.
        model: Modelo LLM. Si es None sale de `settings`.
        system_prompt_path: Ruta al system prompt. Si es None se elige por variante.
        use_chunking: Si True manda a cada sección solo su parte del documento (~75% menos);
            con documento entero, siempre el documento completo.
        parallel: Si True lanza las secciones en paralelo.
        on_section_done: Callback `(nombre, tokens_in, tokens_out)` al completar cada sección.
        on_progress: Callback `(hechas, total, nombre)` para un tracker de avance en vivo. Se
            invoca al arrancar con `(0, total, "")` y tras cada sección completada. Solo cuenta las
            que salen bien; una sección fallida no incrementa `hechas`.
    """
    if not md_text.strip():
        raise ValueError("Markdown vacío")

    if model is None:
        model = get_llm_model()

    get_llm_max_parallel_workers()

    enrutada, tokens_enrutado = (
        (detect_document_type(md_text), {"input": 0, "output": 0})
        if system_prompt_path is not None
        else enrutado or resolver_doc_type(md_text, model)
    )
    variante = _resolver_variante(enrutada, system_prompt_path)
    logger.info("Tipo detectado: %s", variante.variante)

    doc_sections = split_document(md_text) if use_chunking else None
    if doc_sections is not None:
        logger.info(
            "Documento dividido: %d secciones, categorías: %s",
            len(doc_sections),
            {s.category.value for s in doc_sections},
        )

    acumulador = _Acumulador(
        input_tokens=tokens_enrutado["input"], output_tokens=tokens_enrutado["output"]
    )
    total_chars = len(md_text)
    idioma = detectar_idioma(md_text)
    logger.info("Idioma detectado: %s", idioma)

    chunks_por_seccion = {
        key: _chunk_de_seccion(variante, key, info, doc_sections, md_text)
        for key, info in variante.sections.items()
    }
    fidelity = (
        build_fidelity_report(variante.variante, md_text, doc_sections, chunks_por_seccion)
        if doc_sections is not None
        else None
    )

    def _generar(section_key: str, section_info: SectionSpec) -> tuple[str, dict[str, Any], int]:
        chunk = chunks_por_seccion[section_key].text

        def _generar_una(ventana: str, tramo: str = "") -> dict[str, Any]:
            return generate_section_json(
                md_text=ventana,
                section_key=section_key,
                model=model,
                system_prompt_text=variante.system_prompt,
                prompt_builder=lambda clave, texto: variante.builder(clave, texto) + tramo,
                idioma=idioma,
                tablas_en_rejilla=tablas_en_rejilla,
            )

        datos = (
            _generar_apartados(chunk, _generar_una)
            if get_llm_whole_document() and section_key == CLAVE_DOCUMENTO_POR_APARTADOS
            else _generar_por_ventanas(chunk, _generar_una)
        )
        nombre = section_info["name"]
        return nombre, _solo_campos_declarados(nombre, datos, section_info["fields"]), len(chunk)

    total_secciones = len(variante.sections)
    hechas = 0
    if on_progress is not None:
        on_progress(0, total_secciones, "")

    def _recoger(resultado: tuple[str, dict[str, Any], int]) -> None:
        nonlocal hechas
        nombre, datos, chunk_chars = resultado
        tokens = datos.get("_tokens", {})
        tokens_in, tokens_out = tokens.get("input", 0), tokens.get("output", 0)
        acumulador.acumular(nombre, datos, chunk_chars, total_chars)
        if on_section_done is not None:
            on_section_done(nombre, tokens_in, tokens_out)
        # `_recoger` corre en el hilo que orquesta (ver `_Acumulador`), así que el contador no
        # necesita candado ni en modo paralelo.
        hechas += 1
        if on_progress is not None:
            on_progress(hechas, total_secciones, nombre)

    _ejecutar_secciones(
        variante.sections,
        _generar,
        _recoger,
        acumulador.registrar_fallo,
        parallel=parallel,
    )

    _cuadrar_con_la_fuente(
        acumulador, md_text, variante, model, idioma, fidelity, tablas_en_rejilla
    )

    return _cerrar(acumulador, variante, model, fidelity, md_text, doc_sections)


def _cuadrar_con_la_fuente(
    acumulador: _Acumulador,
    md_text: str,
    variante: _Variante,
    model: str,
    idioma: str,
    fidelity: dict[str, Any] | None,
    tablas_en_rejilla: str,
) -> None:
    if get_llm_whole_document():
        generar = _generador_de_cifras(variante, model, idioma, tablas_en_rejilla)
        if not _completar_cifras(acumulador, md_text, generar) and fidelity is not None:
            fidelity["numbers_completion"] = {"status": "unavailable"}
        _revisar_sentido(acumulador, md_text, variante, model, idioma, fidelity, tablas_en_rejilla)
        _quitar_jerga(acumulador, md_text, variante, model, idioma, fidelity, tablas_en_rejilla)
        return
    _cuadrar_cifras(acumulador, md_text, variante, model, idioma, fidelity)
    _cuadrar_nombres(acumulador.clinical_json, md_text, variante.variante, fidelity)


def _revisar_sentido(
    acumulador: _Acumulador,
    md_text: str,
    variante: _Variante,
    model: str,
    idioma: str,
    fidelity: dict[str, Any] | None,
    tablas_en_rejilla: str,
) -> None:
    datos = acumulador.clinical_json
    documento_entero.corregir_comparadores_en(datos)
    if len(md_text) > CHUNK_MAX_CHARS:
        _estado_de_la_revision(fidelity, "skipped")
        return
    tarea = revision_de_sentido.tarea(datos)
    try:
        contenido = _consultar_documento(
            acumulador, md_text, variante, model, idioma, tablas_en_rejilla, tarea, "sense_review"
        )
        corregidas = revision_de_sentido.aplicar(datos, contenido, md_text)
    except ContextoInsuficiente:
        _estado_de_la_revision(fidelity, "skipped", ContextoInsuficiente.__name__)
        return
    except Exception as exc:  # noqa: BLE001 - el JSON ya está completo sin la revisión
        logger.warning("Revisión de sentido no disponible: %s", type(exc).__name__)
        _estado_de_la_revision(fidelity, "unavailable", type(exc).__name__)
        return
    documento_entero.corregir_comparadores_en(datos)
    logger.info("Revisión de sentido: %d correcciones", len(corregidas))
    if fidelity is not None and corregidas:
        fidelity["sense_corrections"] = [
            _registro_de_correccion(
                c.ruta, c.antes, documento_entero.corregir_comparadores(c.despues)
            )
            for c in corregidas
        ]


def _consultar_documento(
    acumulador: _Acumulador,
    md_text: str,
    variante: _Variante,
    model: str,
    idioma: str,
    tablas_en_rejilla: str,
    tarea: str,
    paso: str,
) -> str:
    sistema = _sistema_de_documento_entero(variante.system_prompt)
    tarea = _con_idioma(tarea, idioma)
    documento, sin_rejilla = _documento_que_cabe(sistema, md_text, tablas_en_rejilla, tarea)
    if sin_rejilla:
        acumulador.sin_rejilla.add(paso)
    mensajes = cast(
        "list[ChatCompletionMessageParam]",
        documento_entero.mensajes(sistema, documento, tarea, model),
    )
    respuesta = _call_llm_with_retry(
        model, mensajes, extra=documento_entero.parametros_de_cache(model, documento)
    )
    contenido = respuesta.choices[0].message.content or ""
    acumulador.input_tokens += count_tokens(sistema + documento + tarea)
    acumulador.output_tokens += count_tokens(contenido)
    return contenido


def _quitar_jerga(
    acumulador: _Acumulador,
    md_text: str,
    variante: _Variante,
    model: str,
    idioma: str,
    fidelity: dict[str, Any] | None,
    tablas_en_rejilla: str,
) -> None:
    datos = acumulador.clinical_json
    senaladas = dict(jerga_del_proceso.textos_con_jerga(datos))
    if not senaladas:
        return
    corregidas: list[revision_de_sentido.Correccion] = []
    try:
        if len(md_text) <= CHUNK_MAX_CHARS:
            contenido = _consultar_documento(
                acumulador,
                md_text,
                variante,
                model,
                idioma,
                tablas_en_rejilla,
                jerga_del_proceso.tarea(senaladas),
                "process_wording",
            )
            corregidas = jerga_del_proceso.aplicar(datos, contenido, senaladas)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Corrección de la jerga no disponible: %s", type(exc).__name__)
    restantes = [ruta for ruta, _ in jerga_del_proceso.textos_con_jerga(datos)]
    logger.info("Jerga del proceso: %d corregidas, %d quedan", len(corregidas), len(restantes))
    if fidelity is None:
        return
    if corregidas:
        fidelity["process_wording_corrections"] = [
            _registro_de_correccion(c.ruta, c.antes, c.despues) for c in corregidas
        ]
    if restantes:
        fidelity["process_wording"] = restantes


def _registro_de_correccion(ruta: str, antes: str, despues: str) -> dict[str, Any]:
    comun = len(os.path.commonprefix([antes, despues]))
    desde = max(0, comun - CONTEXTO_ANTES_DEL_CAMBIO)
    return {"field": ruta, "before": _recorte(antes[desde:]), "after": _recorte(despues[desde:])}


def _anotar_rejilla_omitida(fidelity: dict[str, Any] | None, secciones: set[str]) -> None:
    if fidelity is not None and secciones:
        fidelity["table_grid"] = {"omitted_in": sorted(secciones)}


def _estado_de_la_revision(
    fidelity: dict[str, Any] | None, estado: str, error: str | None = None
) -> None:
    if fidelity is not None:
        fidelity["sense_review"] = {"status": estado, "error": error}


def _anclaje_seguro(
    md_text: str, clinical_json: ClinicalJson, variante: DocVariant
) -> dict[str, Any]:
    """Capa 2 del revisor con guarda: informa, nunca bloquea la ingesta.

    `output_anchoring` es la primera llamada a `field_provenance` en el camino de producción, y esa
    función falla con `KeyError` ante un desajuste de esquema —deseable como oráculo en tests y en
    otros consumidores, pero un informe no debe tumbar la conversión de un documento—. Cualquier
    fallo del anclaje se registra y se declara en el propio informe (visible en `/fidelity/`) en vez
    de propagarse: es el contrato del revisor, que informa y deja decidir al llamante. Solo el tipo
    de la excepción entra en el informe, nunca su mensaje, que podría arrastrar texto del documento.
    """
    try:
        return output_anchoring(md_text, clinical_json, variante)
    except Exception as exc:  # noqa: BLE001 - el revisor informa, no bloquea la ingesta
        logger.exception("El anclaje de fidelidad falló; el documento se procesa igual")
        return {"error": type(exc).__name__}


def _recall_seguro(
    clinical_json: ClinicalJson, doc_sections: list[DocumentSection], variante: DocVariant
) -> dict[str, Any]:
    try:
        return salient_recall(clinical_json, doc_sections, variante)
    except Exception as exc:  # noqa: BLE001 - el revisor informa, no bloquea la ingesta
        logger.exception("La capa 3 de fidelidad falló; el documento se procesa igual")
        return {"error": type(exc).__name__}


def _estampar_idioma(clinical_json: ClinicalJson, md_text: str) -> None:
    """Homogeneíza `language` a ISO 639-1 allí donde el esquema ya lo declara.

    Los papers lo llevan en la raíz; las guías, dentro de `metadata`. Solo se toca si la clave ya
    existe: igual que `extraction_date`, inventarle el idioma a una sección que falló taparía la
    señal de `_extraction`. Se prefiere el valor que dejó el modelo, normalizado, y solo si no lo
    dejó cae al idioma que el motor detectó —el mismo que ya se le inyectó en el prompt—; si tampoco
    hay detección fiable, la clave se queda como estaba.
    """
    detectado = ISO_POR_IDIOMA.get(detectar_idioma(md_text))
    contenedores = (clinical_json, clinical_json.get("metadata"))
    for contenedor in contenedores:
        if not isinstance(contenedor, dict) or "language" not in contenedor:
            continue
        elegido = normalizar_idioma(contenedor["language"]) or detectado
        if elegido:
            contenedor["language"] = elegido


def _cerrar(
    acumulador: _Acumulador,
    variante: _Variante,
    model: str,
    fidelity: dict[str, Any] | None,
    md_text: str,
    doc_sections: list[DocumentSection] | None,
) -> ClinicalJson:
    """Saneado, validación, enriquecimiento y los sellos de procedencia del motor.

    `_extraction` se retira antes de escribirlo aunque `_solo_campos_declarados` ya lo filtre
    aguas arriba: aquí es donde vive el invariante de que esa clave la pone el motor y solo el
    motor, y de él depende que el llamante no indexe un documento incompleto. El saneador y el
    enriquecedor también devuelven diccionarios, así que el único sitio donde la ausencia de la
    clave se puede afirmar es el último antes del `return`. `_fidelity` lleva el mismo
    invariante: lo estampa el motor, nunca el LLM, y el punto de entrada lo separa del JSON
    clínico para que el informe no viaje dentro de lo que se archiva y se trocea.

    `schema_version` y `metadata.extraction_date` los estampa el código, nunca el LLM: cuando los
    pedía el prompt de guía, el corpus salió con fechas inventadas de 2025 y con `"1.0"` mientras
    el chunker declaraba `rich-flat-v2`. Un campo de procedencia escrito por el modelo es una
    procedencia inventada. La fecha solo se estampa si el bloque `metadata` existe como objeto:
    inventarle un bloque a un documento cuya sección de metadatos falló taparía justo la señal
    que `_extraction` lleva.
    """
    total = acumulador.input_tokens + acumulador.output_tokens
    cost_info = cost_for_model(acumulador.input_tokens, acumulador.output_tokens, model)
    logger.info(
        "Tokens: %s in + %s out = %s",
        f"{acumulador.input_tokens:,}",
        f"{acumulador.output_tokens:,}",
        f"{total:,}",
    )
    if cost_info is not None:
        logger.info("Coste estimado (%s): $%.6f", model, cost_info["total_cost"])

    _anotar_rejilla_omitida(fidelity, acumulador.sin_rejilla)
    clinical_json = sanitize_clinical_json(acumulador.clinical_json, variante.variante)
    if variante.variante == "guia_clinica":
        renumerar_ids(clinical_json)
        resolver_referencias(clinical_json)
    log_validation_results(
        validate_clinical_json(clinical_json, variante.variante), variante.variante
    )

    clinical_json["schema_version"] = SCHEMA_VERSION
    if variante.variante == "guia_clinica":
        metadata = clinical_json.get("metadata")
        if isinstance(metadata, dict):
            metadata["extraction_date"] = timezone.localdate().isoformat()

    _estampar_idioma(clinical_json, md_text)

    clinical_json.pop("_extraction", None)
    if acumulador.fallidas:
        clinical_json["_extraction"] = {
            "status": "partial",
            "failed_sections": sorted(acumulador.fallidas),
            "errors": dict(sorted(acumulador.fallidas.items())),
        }

    clinical_json.pop("_content_check", None)
    if not get_llm_whole_document() and (
        violaciones := revisar_contenido(clinical_json, variante.variante, md_text)
    ):
        clinical_json["_content_check"] = {"status": "invalid", "violations": violaciones}

    clinical_json.pop("_fidelity", None)
    if fidelity is not None and doc_sections is not None:
        fidelity["anchoring"] = _anclaje_seguro(md_text, clinical_json, variante.variante)
        fidelity["salient"] = _recall_seguro(clinical_json, doc_sections, variante.variante)
        clinical_json["_fidelity"] = fidelity

    if not get_llm_whole_document():
        incorporar_tablas(clinical_json, md_text, variante.variante)
    if variante.variante == "guia_clinica":
        renumerar_ids(clinical_json)

    clinical_json["_usage"] = {
        "model": model,
        "extracted_at": timezone.now().isoformat(),
        "input_tokens": acumulador.input_tokens,
        "output_tokens": acumulador.output_tokens,
        "total_tokens": total,
        "cost": cost_info,
    }

    return clinical_json