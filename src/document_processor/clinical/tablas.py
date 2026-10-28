from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from typing import Any

from src.document_processor.extraction.redaction import MARCADOR
from src.document_processor.schema import ClinicalJson, DocVariant

from .contenido import fusionar_salidas
from .tablas_markdown import TablaFuente, tablas_del_markdown

_ESPACIOS_RE = re.compile(r"\s+")
COINCIDENCIA_MINIMA = 0.5
_COLECCION_DE_TABLAS: dict[DocVariant, str] = {"paper": "tables", "guia_clinica": "artifacts"}
TIPOS_SIN_DATOS_DE_PACIENTE = frozenset(
    {
        "rct",
        "meta_analysis",
        "systematic_review",
        "cohort",
        "narrative_review",
        "editorial",
        "review",
        "review_article",
        "guideline",
        "research",
        "research_article",
        "original_research",
        "original_research_paper",
        "clinical_study",
        "clinical_science",
    }
)
_SEPARADORES_DE_TIPO_RE = re.compile(r"[\s-]+")
_CAMPO_DE_DATOS = "data"
_CAMPO_DE_TITULO = "title"

logger = logging.getLogger(__name__)


def _clave(celda: Any) -> str:
    return _ESPACIOS_RE.sub(" ", str(celda)).strip().casefold()


def _celdas_de(datos: Any) -> set[str]:
    if not isinstance(datos, dict):
        return set()
    filas = [datos.get("columns"), *(datos.get("rows") or [])]
    return {_clave(c) for fila in filas if isinstance(fila, list) for c in fila if _clave(c)}


def _sin_repetir(tablas: list[TablaFuente]) -> list[TablaFuente]:
    vistas: set[str] = set()
    unicas = []
    for tabla in tablas:
        huella = json.dumps(tabla.datos, ensure_ascii=False)
        if huella not in vistas:
            vistas.add(huella)
            unicas.append(tabla)
    return unicas


def _mismo_titulo(entrada: dict[str, Any], tabla: TablaFuente) -> bool:
    titulo = entrada.get(_CAMPO_DE_TITULO)
    return bool(tabla.titulo and isinstance(titulo, str) and _clave(titulo) == _clave(tabla.titulo))


def _parejas(
    entradas: list[dict[str, Any]], tablas: list[TablaFuente]
) -> list[tuple[float, int, int]]:
    celdas = [_celdas_de(e.get(_CAMPO_DE_DATOS)) for e in entradas]
    indice: dict[str, list[int]] = defaultdict(list)
    for i, propias in enumerate(celdas):
        for celda in propias:
            indice[celda].append(i)
    parejas = []
    for j, tabla in enumerate(tablas):
        comunes = Counter(i for celda in _celdas_de(tabla.datos) for i in indice.get(celda, ()))
        for i, n in comunes.items():
            if n / len(celdas[i]) >= COINCIDENCIA_MINIMA:
                parejas.append((n / len(celdas[i]) + _mismo_titulo(entradas[i], tabla), i, j))
    return sorted(parejas, key=lambda p: (-p[0], p[1], p[2]))


def _entrada_nueva(tabla: TablaFuente, variante: DocVariant) -> dict[str, Any]:
    if variante == "paper":
        return {"title": tabla.titulo, "data": tabla.datos, "summary": None}
    return {
        "type": "table",
        "title": tabla.titulo,
        "source_heading": tabla.encabezado,
        "data": tabla.datos,
        "description": None,
        "summary": None,
        "clinical_relevance": None,
    }


def _tipo_de_articulo(datos: ClinicalJson) -> str | None:
    tipo = datos.get("article_type")
    if not isinstance(tipo, str):
        return None
    return _SEPARADORES_DE_TIPO_RE.sub("_", tipo.strip().casefold())


def admite_copia_literal(datos: ClinicalJson, variante: DocVariant) -> bool:
    if MARCADOR in json.dumps(datos, ensure_ascii=False):
        return False
    return variante == "guia_clinica" or _tipo_de_articulo(datos) in TIPOS_SIN_DATOS_DE_PACIENTE


def _sin_datos(entrada: dict[str, Any]) -> dict[str, Any]:
    return {clave: valor for clave, valor in entrada.items() if clave != _CAMPO_DE_DATOS}


def incorporar_tablas(datos: ClinicalJson, md: str, variante: DocVariant) -> None:
    coleccion = _COLECCION_DE_TABLAS.get(variante)
    if coleccion is None:
        return
    if not admite_copia_literal(datos, variante):
        logger.info("Tablas no copiadas del texto (article_type %r)", datos.get("article_type"))
        return
    actuales = datos.get(coleccion)
    entradas: list[Any] = actuales if isinstance(actuales, list) else []
    del_llm = [e for e in entradas if isinstance(e, dict)]
    tablas = _sin_repetir(tablas_del_markdown(md))
    destino: dict[int, dict[str, Any]] = {}
    asignadas: set[int] = set()
    mejor_tabla: dict[int, int] = {}
    for _, i, j in _parejas(del_llm, tablas):
        mejor_tabla.setdefault(i, j)
        if j not in destino and i not in asignadas:
            destino[j] = del_llm[i]
            asignadas.add(i)
    for j, tabla in enumerate(tablas):
        if j not in destino:
            destino[j] = _entrada_nueva(tabla, variante)
            entradas.append(destino[j])
        else:
            destino[j][_CAMPO_DE_DATOS] = tabla.datos
    absorbidas: set[int] = set()
    for i, j in mejor_tabla.items():
        parcial = del_llm[i]
        if i in asignadas:
            continue
        fusionar_salidas(destino[j], _sin_datos(parcial))
        absorbidas.add(id(parcial))
    datos[coleccion] = [e for e in entradas if id(e) not in absorbidas]