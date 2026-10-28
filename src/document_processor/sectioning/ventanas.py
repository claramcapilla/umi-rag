from __future__ import annotations

import re

_CORTE_DE_ENCABEZADO_RE = re.compile(r"\n(?=#{1,6} )")
_CORTE_DE_PARRAFO_RE = re.compile(r"\n\s*\n")
_CORTE_DE_LINEA_RE = re.compile(r"\n")
_CORTE_DE_PALABRA_RE = re.compile(r"\s")
_CORTES = (_CORTE_DE_ENCABEZADO_RE, _CORTE_DE_PARRAFO_RE, _CORTE_DE_LINEA_RE, _CORTE_DE_PALABRA_RE)


def _corte(texto: str, inicio: int, max_chars: int) -> int:
    limite = inicio + max_chars
    minimo = inicio + max_chars // 2
    for patron in _CORTES:
        cortes = [m.end() for m in patron.finditer(texto, minimo, limite)]
        if cortes:
            return cortes[-1]
    return limite


def partir_en_ventanas(texto: str, max_chars: int) -> list[str]:
    if max_chars < 2:
        raise ValueError("Una ventana necesita al menos dos caracteres.")
    ventanas = []
    inicio = 0
    while len(texto) - inicio > max_chars:
        fin = _corte(texto, inicio, max_chars)
        ventanas.append(texto[inicio:fin])
        inicio = fin
    ventanas.append(texto[inicio:])
    return ventanas