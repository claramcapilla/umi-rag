from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from src.document_processor.schema import ClinicalJson, DocVariant

_PALABRA_RE = re.compile(r"[a-z0-9]+")
_POSESIVO_RE = re.compile(r"['’]s\b")
_FIN_DE_FRASE_RE = re.compile(r"(?:[.!?;](?=\s)|\n)+")
_GRIEGAS = str.maketrans(
    {
        "α": " alpha ",
        "β": " beta ",
        "γ": " gamma ",
        "δ": " delta ",
        "κ": " kappa ",
        "λ": " lambda ",
        "µ": " mu ",
        "μ": " mu ",
    }
)
_CONECTORES = frozenset({"and", "or", "y", "o"})
_SEPARADORES_DE_LISTA = (",", ";")
_ELEMENTO = "[]"


class Accion(StrEnum):
    QUITAR_ELEMENTO = "element_removed"
    REGISTRAR = "recorded"


@dataclass(frozen=True)
class Campo:
    ruta: tuple[str, ...]
    clave: str | None
    accion: Accion

    @property
    def nombre(self) -> str:
        return ".".join((*self.ruta, self.clave) if self.clave else self.ruta)


@dataclass(frozen=True)
class NombreSinFuente:
    campo: Campo
    nombre: str
    lista: list[Any]
    indice: int


def _campo(ruta: str, accion: Accion) -> Campo:
    pasos = tuple(ruta.split("."))
    if pasos[-1] == _ELEMENTO:
        return Campo(pasos, None, accion)
    return Campo(pasos[:-1], pasos[-1], accion)


_QUITAR_ELEMENTO = Accion.QUITAR_ELEMENTO
_REGISTRAR = Accion.REGISTRAR

CAMPOS: dict[str, tuple[Campo, ...]] = {
    "guia_clinica": tuple(
        _campo(ruta, accion)
        for ruta, accion in (
            ("conditions.[].name", _REGISTRAR),
            ("conditions.[].subtypes.[].name", _REGISTRAR),
            ("therapies_index.[].name", _REGISTRAR),
            ("conditions.[].synonyms.[]", _QUITAR_ELEMENTO),
            ("scope.conditions_covered.[]", _QUITAR_ELEMENTO),
            ("conditions.[].follow_up.recommended_tests.[]", _QUITAR_ELEMENTO),
            ("therapies_index.[].components.[]", _QUITAR_ELEMENTO),
            ("therapies_index.[].key_toxicities.[]", _QUITAR_ELEMENTO),
            ("therapies_index.[].monitoring.[]", _QUITAR_ELEMENTO),
            ("safety.toxicities.[].associated_therapies.[]", _QUITAR_ELEMENTO),
            ("conditions.[].diagnostic_workup.[].test", _REGISTRAR),
            ("conditions.[].pathology_markers.[].marker", _REGISTRAR),
            ("conditions.[].genetic_molecular.[].marker", _REGISTRAR),
            ("conditions.[].prognostic_tools.[].name", _REGISTRAR),
            ("safety.toxicities.[].event", _REGISTRAR),
            ("treatment_pathways.[].recommended_regimens.[].name", _REGISTRAR),
            ("treatment_pathways.[].alternative_regimens.[].name", _REGISTRAR),
            ("treatment_pathways.[].not_recommended.[].name", _REGISTRAR),
        )
    ),
    "paper": tuple(
        _campo(ruta, accion)
        for ruta, accion in (
            ("diseases_and_conditions.[].name", _REGISTRAR),
            ("drugs_and_therapies.[].name", _REGISTRAR),
            ("biomarkers_and_genes.[].name", _REGISTRAR),
            ("scales_and_classifications.[].name", _REGISTRAR),
            ("interventions.[]", _QUITAR_ELEMENTO),
            ("comparators.[]", _QUITAR_ELEMENTO),
        )
    ),
}


def _singular(palabra: str) -> str:
    if palabra.endswith("ies") and len(palabra) > 4:
        palabra = f"{palabra[:-3]}y"
    elif palabra.endswith("s") and len(palabra) > 3:
        palabra = palabra[:-1]
    if palabra.endswith("e") and len(palabra) > 3:
        palabra = palabra[:-1]
    return palabra


def _palabras(texto: str) -> list[str]:
    sin_acentos = unicodedata.normalize("NFKD", _POSESIVO_RE.sub("", texto).translate(_GRIEGAS))
    plano = "".join(c for c in sin_acentos if not unicodedata.combining(c)).casefold()
    return [_singular(p) for p in _PALABRA_RE.findall(plano) if p not in _CONECTORES]


def _frase(texto: str) -> str:
    return " ".join(_palabras(texto))


def _es_sigla_de(sigla: str, nombre: str) -> bool:
    letras = iter(palabra[0] for palabra in _palabras(nombre))
    mayusculas = [letra.casefold() for letra in sigla if letra.isupper()]
    return len(mayusculas) > 1 and all(letra in letras for letra in mayusculas)


def _formas(nombre: str) -> Iterator[str]:
    yield nombre
    antes, abre, resto = nombre.partition("(")
    dentro, cierra, despues = resto.partition(")")
    if abre and cierra:
        yield f"{antes} {despues}"
        if _es_sigla_de(dentro, antes):
            yield dentro


def _partes(nombre: str) -> list[str]:
    partes = [nombre]
    for separador in _SEPARADORES_DE_LISTA:
        partes = [trozo for parte in partes for trozo in parte.split(separador)]
    return [parte for parte in partes if parte.strip()]


def _ocurrencias(nodo: Any, ruta: tuple[str, ...]) -> Iterator[tuple[list[Any], int, Any]]:
    paso, resto = ruta[0], ruta[1:]
    if paso == _ELEMENTO:
        if not isinstance(nodo, list):
            return
        for indice, valor in enumerate(nodo):
            if resto:
                yield from _ocurrencias(valor, resto)
            else:
                yield nodo, indice, valor
    elif isinstance(nodo, dict) and paso in nodo:
        yield from _ocurrencias(nodo[paso], resto)


def _nombre_de(valor: Any, clave: str | None) -> str | None:
    if clave is not None:
        valor = valor.get(clave) if isinstance(valor, dict) else None
    return valor if isinstance(valor, str) and valor.strip() else None


class RevisorDeNombres:
    def __init__(self, md: str) -> None:
        self._documento = f" {_frase(md)} "
        self._frases_con: dict[str, set[int]] = {}
        for numero, frase in enumerate(_FIN_DE_FRASE_RE.split(md)):
            for palabra in _palabras(frase):
                self._frases_con.setdefault(palabra, set()).add(numero)

    def _en_el_documento(self, texto: str) -> bool:
        frase = _frase(texto)
        return bool(frase) and f" {frase} " in self._documento

    def _en_una_frase(self, texto: str) -> bool:
        palabras = set(_palabras(texto))
        frases = [self._frases_con.get(palabra, set()) for palabra in palabras]
        return bool(frases) and bool(set.intersection(*frases))

    def con_fuente(self, nombre: str) -> bool:
        if any(self._en_el_documento(forma) for forma in _formas(nombre)):
            return True
        if self._en_una_frase(nombre):
            return True
        partes = _partes(nombre)
        return len(partes) > 1 and all(self.con_fuente(parte) for parte in partes)

    def sin_fuente(self, datos: ClinicalJson, variante: DocVariant) -> list[NombreSinFuente]:
        hallados = []
        for campo in CAMPOS.get(variante, ()):
            for lista, indice, valor in _ocurrencias(datos, campo.ruta):
                nombre = _nombre_de(valor, campo.clave)
                if nombre is not None and not self.con_fuente(nombre):
                    hallados.append(NombreSinFuente(campo, nombre, lista, indice))
        return hallados


def quitar_nombres_sin_fuente(
    datos: ClinicalJson, variante: DocVariant, revisor: RevisorDeNombres
) -> list[NombreSinFuente]:
    hallados = revisor.sin_fuente(datos, variante)
    por_lista: dict[int, tuple[list[Any], set[int]]] = {}
    for hallado in hallados:
        if hallado.campo.accion != Accion.REGISTRAR:
            por_lista.setdefault(id(hallado.lista), (hallado.lista, set()))[1].add(hallado.indice)
    for lista, indices in por_lista.values():
        for indice in sorted(indices, reverse=True):
            del lista[indice]
    return hallados