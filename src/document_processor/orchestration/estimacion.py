from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from document_processor.clinical.cifras import cifras_del_texto
from document_processor.config import CHUNK_MAX_CHARS
from document_processor.orchestration import documento_entero
from document_processor.orchestration.clinical_formatter import sistema_y_secciones
from document_processor.schema import DocVariant
from document_processor.sectioning.ventanas import partir_en_ventanas
from document_processor.token_cost_utils import CONTEXTO_LARGO_TOKENS, coste, count_tokens

SOBRECOSTE_DE_REJILLA_DESCONOCIDA = 0.139
SALIDA_POR_TOKEN_DE_JSON = 1.065
JSON_FIJO = 2_408
JSON_POR_TOKEN_DEL_MARKDOWN = 0.619
JSON_POR_CIFRA = 101
JSON_DE_GUIA = 24_883
PROMPT_DEL_ENRUTADO = 2_000
LLAMADAS_TRAS_LAS_SECCIONES = 2


@dataclass(frozen=True)
class Estimacion:
    llamadas: int = 0
    prompt: int = 0
    en_cache: int = 0
    salida: int = 0
    mayor_prompt: int = 0
    coste: float | None = 0.0

    def __add__(self, otra: Estimacion) -> Estimacion:
        return Estimacion(
            llamadas=self.llamadas + otra.llamadas,
            prompt=self.prompt + otra.prompt,
            en_cache=self.en_cache + otra.en_cache,
            salida=self.salida + otra.salida,
            mayor_prompt=max(self.mayor_prompt, otra.mayor_prompt),
            coste=_sumar_costes(self.coste, otra.coste),
        )


def json_esperado(md: str, variante: DocVariant) -> int:
    return round(
        JSON_FIJO
        + JSON_POR_TOKEN_DEL_MARKDOWN * count_tokens(md)
        + JSON_POR_CIFRA * len(cifras_del_texto(md))
        + (JSON_DE_GUIA if variante == "guia_clinica" else 0)
    )


def _llamadas_de_apartados(md: str) -> int:
    return sum(
        max(1, len(documento_entero.tramos(ventana)))
        for ventana in partir_en_ventanas(md, CHUNK_MAX_CHARS)
    )


def _tokens_del_documento(md: str, rejilla: str | None) -> int:
    documento = count_tokens(documento_entero.documento_con_bloques(md, rejilla or ""))
    if rejilla is None:
        return round(documento * (1 + SOBRECOSTE_DE_REJILLA_DESCONOCIDA))
    return documento


@cache
def _sistema(variante: DocVariant) -> tuple[int, int]:
    sistema, secciones = sistema_y_secciones(variante)
    return count_tokens(sistema), secciones


def _sumar_costes(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


def estimar(
    md: str,
    variante: DocVariant,
    *,
    rejilla: str | None,
    enrutar: bool,
    model: str,
    nivel: str | None,
) -> Estimacion:
    sistema, secciones = _sistema(variante)
    por_llamada = sistema + _tokens_del_documento(md, rejilla)
    json = json_esperado(md, variante)
    llamadas = secciones - 1 + _llamadas_de_apartados(md) + LLAMADAS_TRAS_LAS_SECCIONES
    revision = por_llamada + json
    salida = round(SALIDA_POR_TOKEN_DE_JSON * json)
    prompt = llamadas * por_llamada + json
    en_cache = (llamadas - 1) * por_llamada
    precio = _sumar_costes(
        coste(
            model,
            nivel,
            prompt=prompt - revision,
            en_cache=en_cache - por_llamada,
            salida=0,
            largo=por_llamada > CONTEXTO_LARGO_TOKENS,
        ),
        coste(
            model,
            nivel,
            prompt=revision,
            en_cache=por_llamada,
            salida=salida,
            largo=revision > CONTEXTO_LARGO_TOKENS,
        ),
    )
    if enrutar:
        llamadas += 1
        prompt += PROMPT_DEL_ENRUTADO
        precio = _sumar_costes(
            precio,
            coste(model, nivel, prompt=PROMPT_DEL_ENRUTADO, en_cache=0, salida=0, largo=False),
        )
    return Estimacion(
        llamadas=llamadas,
        prompt=prompt,
        en_cache=en_cache,
        salida=salida,
        mayor_prompt=revision,
        coste=precio,
    )