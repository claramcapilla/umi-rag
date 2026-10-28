from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from typing import Any

from src.document_processor.schema import ClinicalJson, DocVariant

from .cifras_sin_fuente import cifras_sin_fuente
from .referencias import referencias_rotas

UMBRAL_DE_BUCLE = 5
MAX_PALABRAS_CLAVE_GENERADAS = 20
CLAVE_FILAS = "rows"
CLAVES_POSICIONALES = frozenset({"columns", "heading_path"})
CADENA_NULA = "null"
_SUFIJO_DE_ID = "_id"
MARCADORES_DE_AUSENCIA = frozenset({"N/A", "n/a", "NA", "N.A.", "-", "--", "—", CADENA_NULA})
CLAVES_DE_IDENTIDAD = frozenset(
    {
        "title",
        "journal",
        "year",
        "doi",
        "trial_registration",
        "article_type",
        "article_subtype",
        "language",
        "document_type",
        "version",
        "audience",
        "n_patients",
        "n_studies_included",
        "source_pages",
        "name",
    }
)
CLAVE_DE_NOMBRE = "name"
SEPARADOR_DE_TEXTOS = "\n\n"


def _huella(elemento: Any) -> str:
    if isinstance(elemento, dict):
        elemento = {k: v for k, v in elemento.items() if not k.endswith(_SUFIJO_DE_ID)}
    return json.dumps(elemento, sort_keys=True, ensure_ascii=False)


def _vacio(elemento: Any) -> bool:
    if isinstance(elemento, str):
        return not elemento.strip() or elemento.strip() in MARCADORES_DE_AUSENCIA
    if isinstance(elemento, list):
        return all(_vacio(valor) for valor in elemento)
    if isinstance(elemento, dict):
        return all(_vacio(valor) for valor in elemento.values())
    return elemento is None


def _listas(nodo: Any, ruta: str = "") -> Iterator[tuple[str, list[Any], bool]]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            sub = f"{ruta}.{clave}" if ruta else clave
            if clave.startswith("_") or clave in CLAVES_POSICIONALES:
                continue
            if clave == CLAVE_FILAS and isinstance(valor, list):
                yield sub, valor, True
                continue
            yield from _listas(valor, sub)
    elif isinstance(nodo, list):
        yield ruta, nodo, False
        for valor in nodo:
            yield from _listas(valor, ruta)


def _mas_repetido(lista: list[Any]) -> int:
    huellas = Counter(_huella(e) for e in lista if not _vacio(e))
    return max(huellas.values(), default=0)


def _mas_repetido_seguido(lista: list[Any]) -> int:
    mayor = racha = 0
    anterior = None
    for elemento in lista:
        huella = None if _vacio(elemento) else _huella(elemento)
        racha = racha + 1 if huella is not None and huella == anterior else 1
        anterior = huella
        if huella is not None:
            mayor = max(mayor, racha)
    return mayor


def ruta_en_bucle(datos: ClinicalJson) -> str | None:
    return next(
        (
            ruta
            for ruta, lista, filas in _listas(datos)
            if (_mas_repetido_seguido(lista) if filas else _mas_repetido(lista)) >= UMBRAL_DE_BUCLE
        ),
        None,
    )


def quitar_duplicados(datos: ClinicalJson) -> None:
    for _, lista, filas in _listas(datos):
        if filas:
            continue
        vistos: set[str] = set()
        unicos = []
        for elemento in lista:
            huella = _huella(elemento)
            if huella not in vistos:
                vistos.add(huella)
                unicos.append(elemento)
        lista[:] = unicos


def _palabras_clave_generadas(datos: ClinicalJson) -> Iterator[list[Any]]:
    palabras = datos.get("keywords")
    for bloque in palabras if isinstance(palabras, list) else [palabras]:
        if isinstance(bloque, dict) and isinstance(bloque.get("enriched"), list):
            yield bloque["enriched"]


def _como_lista(valor: Any) -> list[Any]:
    return valor if isinstance(valor, list) else [valor]


def _nombre(elemento: Any) -> str | None:
    if not isinstance(elemento, dict) or not isinstance(elemento.get(CLAVE_DE_NOMBRE), str):
        return None
    return " ".join(elemento[CLAVE_DE_NOMBRE].casefold().split()) or None


def _fusionar_listas(actual: list[Any], nuevo: list[Any]) -> list[Any]:
    resultado = list(actual)
    por_nombre: dict[str, Any] = {}
    for elemento in resultado:
        if nombre := _nombre(elemento):
            por_nombre.setdefault(nombre, elemento)
    for elemento in nuevo:
        nombre = _nombre(elemento)
        if nombre and nombre in por_nombre:
            fusionar_salidas(por_nombre[nombre], elemento)
            continue
        resultado.append(elemento)
        if nombre:
            por_nombre[nombre] = elemento
    return resultado


def _texto_ya_presente(actual: str, nuevo: str) -> bool:
    return nuevo.strip() in {parte.strip() for parte in actual.split(SEPARADOR_DE_TEXTOS)}


def _fusionar_valor(clave: str, actual: Any, nuevo: Any) -> Any:
    if _vacio(actual):
        return nuevo
    if _vacio(nuevo):
        return actual
    if isinstance(actual, dict) and isinstance(nuevo, dict):
        fusionar_salidas(actual, nuevo)
        return actual
    if isinstance(actual, list) or isinstance(nuevo, list):
        return _fusionar_listas(_como_lista(actual), _como_lista(nuevo))
    if clave in CLAVES_DE_IDENTIDAD or actual == nuevo:
        return actual
    if isinstance(actual, str) and isinstance(nuevo, str):
        if _texto_ya_presente(actual, nuevo):
            return actual
        return f"{actual}{SEPARADOR_DE_TEXTOS}{nuevo}"
    return [actual, nuevo]


def fusionar_salidas(destino: dict[str, Any], origen: dict[str, Any]) -> None:
    for clave, nuevo in origen.items():
        destino[clave] = (
            _fusionar_valor(clave, destino[clave], nuevo) if clave in destino else nuevo
        )


def recortar_palabras_clave(datos: ClinicalJson) -> None:
    for generadas in _palabras_clave_generadas(datos):
        del generadas[MAX_PALABRAS_CLAVE_GENERADAS:]


def _cadenas_nulas(nodo: Any, ruta: str = "") -> Iterator[str]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if not clave.startswith("_"):
                yield from _cadenas_nulas(valor, f"{ruta}.{clave}" if ruta else clave)
    elif isinstance(nodo, list):
        for valor in nodo:
            yield from _cadenas_nulas(valor, ruta)
    elif isinstance(nodo, str) and nodo.strip() == CADENA_NULA:
        yield ruta


def revisar_contenido(
    datos: ClinicalJson, variante: DocVariant, md: str | None = None
) -> list[str]:
    violaciones = [
        f"{ruta}: {veces} copias del mismo elemento"
        for ruta, lista, filas in _listas(datos)
        if (veces := _mas_repetido_seguido(lista) if filas else _mas_repetido(lista))
        >= (UMBRAL_DE_BUCLE if filas else 2)
    ]
    violaciones += [
        f"keywords.enriched: {len(generadas)} palabras clave, el máximo es "
        f"{MAX_PALABRAS_CLAVE_GENERADAS}"
        for generadas in _palabras_clave_generadas(datos)
        if len(generadas) > MAX_PALABRAS_CLAVE_GENERADAS
    ]
    violaciones += [
        f"{ruta}: la cadena «null» en lugar de un valor nulo"
        for ruta in sorted(set(_cadenas_nulas(datos)))
    ]
    if variante == "guia_clinica":
        violaciones += referencias_rotas(datos)
    if md is not None:
        violaciones += [
            f"{cifra}: cifra que no está en el documento" for cifra in cifras_sin_fuente(md, datos)
        ]
    return violaciones