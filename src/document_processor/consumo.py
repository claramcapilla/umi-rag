from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from document_processor.token_cost_utils import coste_de_llamada


@dataclass
class Consumo:
    llamadas: int = 0
    prompt: int = 0
    en_cache: int = 0
    salida: int = 0
    razonamiento: int = 0
    coste: float | None = 0.0
    _candado: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def sumar(
        self,
        model: str,
        nivel: str | None,
        prompt: int,
        en_cache: int,
        salida: int,
        razonamiento: int,
    ) -> None:
        coste = coste_de_llamada(model, nivel, prompt, en_cache, salida)
        with self._candado:
            self.llamadas += 1
            self.prompt += prompt
            self.en_cache += en_cache
            self.salida += salida
            self.razonamiento += razonamiento
            self.coste = None if coste is None or self.coste is None else self.coste + coste

    def resumen(self) -> dict[str, Any]:
        with self._candado:
            return {
                "calls": self.llamadas,
                "prompt_tokens": self.prompt,
                "cached_tokens": self.en_cache,
                "completion_tokens": self.salida,
                "reasoning_tokens": self.razonamiento,
                "cost_usd": None if self.coste is None else round(self.coste, 6),
            }


_EN_CURSO: ContextVar[Consumo | None] = ContextVar("consumo_del_llm", default=None)


@contextmanager
def midiendo() -> Iterator[Consumo]:
    consumo = Consumo()
    marca = _EN_CURSO.set(consumo)
    try:
        yield consumo
    finally:
        _EN_CURSO.reset(marca)


def registrar(
    model: str, nivel: str | None, prompt: int, en_cache: int, salida: int, razonamiento: int
) -> None:
    consumo = _EN_CURSO.get()
    if consumo is not None:
        consumo.sumar(model, nivel, prompt, en_cache, salida, razonamiento)