"""Conteo de tokens y coste por modelo.

El tokenizador se memoiza: `encoding_for_model` descarga el fichero BPE de un blob remoto en
el primer uso salvo que exista caché, y se instanciaba dos veces por sección.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import tiktoken


@lru_cache(maxsize=1)
def get_tokenizer() -> Any:
    try:
        return tiktoken.encoding_for_model("gpt-4o")
    except Exception:  # noqa: BLE001 - cualquier fallo cae al encoding genérico
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(get_tokenizer().encode(text, disallowed_special=()))


MODEL_PRICES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.28, "output": 0.42},
    "deepseek-reasoner": {"input": 0.28, "output": 0.42},
    "gpt-5.1": {"input": 0.625, "output": 5.00},
    "gpt-5": {"input": 0.625, "output": 5.00},
    "gpt-5-mini": {"input": 0.125, "output": 1.00},
    "gpt-5-nano": {"input": 0.025, "output": 0.20},
    "gpt-5-pro": {"input": 7.50, "output": 60.00},
    "gpt-4.1": {"input": 1.00, "output": 4.00},
    "gpt-4.1-mini": {"input": 0.20, "output": 0.80},
    "gpt-4.1-nano": {"input": 0.05, "output": 0.20},
    "gpt-4o": {"input": 1.25, "output": 5.00},
    "gpt-4o-2024-05-13": {"input": 2.50, "output": 7.50},
    "gpt-4o-mini": {"input": 0.075, "output": 0.30},
    "o1": {"input": 7.50, "output": 30.00},
    "o1-pro": {"input": 75.00, "output": 300.00},
    "o3-pro": {"input": 10.00, "output": 40.00},
    "o3": {"input": 1.00, "output": 4.00},
    "o3-deep-research": {"input": 5.00, "output": 20.00},
    "o4-mini": {"input": 0.55, "output": 2.20},
    "o4-mini-deep-research": {"input": 1.00, "output": 4.00},
    "o3-mini": {"input": 0.55, "output": 2.20},
    "o1-mini": {"input": 0.55, "output": 2.20},
    "computer-use-preview": {"input": 1.50, "output": 6.00},
}

_PER_MILLION = 1_000_000

CONTEXTO_LARGO_TOKENS = 272_000
_RECARGO_DE_ENTRADA_LARGA = 2.0
_RECARGO_DE_SALIDA_LARGA = 1.5
_FACTOR_POR_NIVEL = {"flex": 0.5, "batch": 0.5}


@dataclass(frozen=True)
class Tarifa:
    entrada: float
    entrada_en_cache: float
    salida: float


TARIFAS: dict[str, Tarifa] = {
    "gpt-6-sol": Tarifa(entrada=2.00, entrada_en_cache=0.20, salida=10.00),
}


def coste(
    model: str, nivel: str | None, *, prompt: int, en_cache: int, salida: int, largo: bool
) -> float | None:
    tarifa = TARIFAS.get(model)
    if tarifa is None:
        return None
    entrada = (prompt - en_cache) * tarifa.entrada + en_cache * tarifa.entrada_en_cache
    total = entrada * (_RECARGO_DE_ENTRADA_LARGA if largo else 1.0) + salida * tarifa.salida * (
        _RECARGO_DE_SALIDA_LARGA if largo else 1.0
    )
    return total * _FACTOR_POR_NIVEL.get(nivel or "", 1.0) / _PER_MILLION


def coste_de_llamada(
    model: str, nivel: str | None, prompt: int, en_cache: int, salida: int
) -> float | None:
    return coste(
        model,
        nivel,
        prompt=prompt,
        en_cache=en_cache,
        salida=salida,
        largo=prompt > CONTEXTO_LARGO_TOKENS,
    )


def cost_for_model(
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> dict[str, float] | None:
    """Coste desglosado en USD, con precios por millón de tokens.

    Devuelve `None` si el modelo no tiene precio registrado.
    """
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return None

    input_cost = input_tokens / _PER_MILLION * prices["input"]
    output_cost = output_tokens / _PER_MILLION * prices["output"]

    return {
        "input_cost": round(input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(input_cost + output_cost, 8),
    }