from __future__ import annotations

import re
from collections import Counter

from src.document_processor.extraction.tablas_en_rejilla import TITULO_DE_TABLA_RE

_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]{4,}")
VOTOS_MINIMOS = 3
VENTAJA_MINIMA = 3


def repartir_tablas(rejilla: str, capitulos: list[str]) -> list[str]:
    bloques = [bloque for bloque in rejilla.split("\n\n") if bloque.strip()]
    tokens = [set(_TOKEN_RE.findall(texto.lower())) for texto in capitulos]
    duenos = _duenos_unicos(tokens)
    titulos = _duenos_unicos(
        [{_titulo(m.group(0)) for m in TITULO_DE_TABLA_RE.finditer(t)} for t in capitulos]
    )
    anclas = [_capitulo_seguro(bloque, titulos, duenos) for bloque in bloques]
    destinos = [
        {ancla} if ancla is not None else _vecinos(anclas, indice, bloque, tokens)
        for indice, (bloque, ancla) in enumerate(zip(bloques, anclas, strict=True))
    ]
    repartidos = list(zip(bloques, destinos, strict=True))
    return [
        "\n\n".join(bloque for bloque, destino in repartidos if i in destino)
        for i in range(len(capitulos))
    ]


def _duenos_unicos(conjuntos: list[set[str]]) -> dict[str, int]:
    frecuencia = Counter(clave for conjunto in conjuntos for clave in conjunto)
    return {
        clave: i
        for i, conjunto in enumerate(conjuntos)
        for clave in conjunto
        if frecuencia[clave] == 1
    }


def _titulo(texto: str) -> str:
    return " ".join(texto.lower().split())


def _capitulo_seguro(bloque: str, titulos: dict[str, int], duenos: dict[str, int]) -> int | None:
    primera = bloque.split("\n", 1)[0]
    titulo = None if primera.lstrip().startswith("|") else TITULO_DE_TABLA_RE.search(primera)
    if titulo and _titulo(titulo.group(0)) in titulos:
        return titulos[_titulo(titulo.group(0))]
    votos = Counter(duenos[t] for t in set(_TOKEN_RE.findall(bloque.lower())) if t in duenos)
    ranking = votos.most_common(2) + [(-1, 0), (-1, 0)]
    (mejor, primero), (_, segundo) = ranking[0], ranking[1]
    if primero >= VOTOS_MINIMOS and primero >= VENTAJA_MINIMA * segundo:
        return mejor
    return None


def _vecinos(
    anclas: list[int | None], indice: int, bloque: str, tokens: list[set[str]]
) -> set[int]:
    seguras = [a for a in anclas if a is not None]
    if not seguras:
        propios = set(_TOKEN_RE.findall(bloque.lower()))
        return {max(range(len(tokens)), key=lambda i: len(propios & tokens[i]))}
    antes = next((a for a in reversed(anclas[:indice]) if a is not None), None)
    despues = next((a for a in anclas[indice + 1 :] if a is not None), None)
    extremos = [a for a in (antes, despues) if a is not None]
    return set(range(min(extremos), max(extremos) + 1))