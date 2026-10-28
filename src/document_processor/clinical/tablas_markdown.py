from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_FILA_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEPARADOR_DE_CELDAS_RE = re.compile(r"(?<!\\)\|")
_CELDA_SEPARADORA_RE = re.compile(r":?-{3,}:?")
_SALTO_HTML_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_ENCABEZADO_RE = re.compile(r"^#{1,6}\s+(\S.*)")
_TITULO_RE = re.compile(r"^\**\s*(?:tabla|table|cuadro|box|recuadro)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TablaFuente:
    titulo: str | None
    encabezado: str | None
    columnas: list[str]
    filas: list[list[str]]

    @property
    def datos(self) -> dict[str, Any]:
        return {"columns": list(self.columnas), "rows": [list(fila) for fila in self.filas]}


def _celdas(linea: str) -> list[str]:
    interior = linea.strip()[1:-1]
    return [
        _SALTO_HTML_RE.sub("\n", celda).replace("\\|", "|").strip()
        for celda in _SEPARADOR_DE_CELDAS_RE.split(interior)
    ]


def _es_separadora(celdas: list[str]) -> bool:
    return all(_CELDA_SEPARADORA_RE.fullmatch(c) or not c for c in celdas) and any(celdas)


def _tabla(lineas: list[str], titulo: str | None, encabezado: str | None) -> TablaFuente:
    filas = [_celdas(linea) for linea in lineas]
    if len(filas) > 1 and _es_separadora(filas[1]):
        return TablaFuente(titulo, encabezado, filas[0], filas[2:])
    return TablaFuente(titulo, encabezado, [], [f for f in filas if not _es_separadora(f)])


def tablas_del_markdown(md: str) -> list[TablaFuente]:
    tablas: list[TablaFuente] = []
    encabezado: str | None = None
    previa: str | None = None
    actual: list[str] = []
    for linea in [*md.split("\n"), ""]:
        if _FILA_RE.match(linea):
            actual.append(linea)
            continue
        if actual:
            titulo = previa if previa and _TITULO_RE.match(previa) else None
            tablas.append(_tabla(actual, titulo, encabezado))
            actual = []
        if m := _ENCABEZADO_RE.match(linea):
            encabezado = m.group(1).strip()
        elif _TITULO_RE.match(linea.strip()) and encabezado and _TITULO_RE.match(encabezado):
            encabezado = None
        if linea.strip():
            previa = linea.strip()
    return [t for t in tablas if any(any(celda for celda in fila) for fila in t.filas)]