from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_UNIDADES = (
    (r"mm ?hg", "mmhg"),
    (r"mmol", "mmol"),
    (r"meq", "meq"),
    (r"[uµμ]mol", "umol"),
    (r"mcg|[uµμ]g", "ug"),
    (r"ng", "ng"),
    (r"mg", "mg"),
    (r"kg", "kg"),
    (r"g", "g"),
    (r"ui|iu", "ui"),
    (r"ml", "ml"),
    (r"dl", "dl"),
    (r"l", "l"),
    (r"%|percent|per cent|por ciento", "%"),
    (r"d[ií]as|days|day|d[ií]a", "day"),
    (r"semanas|weeks|week|semana|wk", "week"),
    (r"meses|months|month|mes", "month"),
    (r"a[ñn]os|years|year|a[ñn]o|yr", "year"),
    (r"horas|hours|hour|hora|hrs|h", "h"),
    (r"minutos|minutes|minute|min", "min"),
    (r"segundos|seconds|second|sec", "s"),
    (r"lpm|bpm|latidos", "bpm"),
    (r"cm", "cm"),
    (r"mm", "mm"),
)
_DENOMINADORES = (
    (r"kg", "kg"),
    (r"m2|m²", "m2"),
    (r"dl", "dl"),
    (r"ml", "ml"),
    (r"l", "l"),
    (r"d[ií]as?|days?|d", "d"),
    (r"horas?|hours?|h", "h"),
    (r"min", "min"),
    (r"semanas?|weeks?|wk", "wk"),
    (r"dosis|doses?", "dosis"),
)
_NUMERO = r"\d+(?:[.,]\d+)*"
_ALTERNATIVAS = "|".join(f"(?:{p})" for p, _ in _UNIDADES)
_ALTERNATIVAS_DE_DENOMINADOR = "|".join(f"(?:{p})" for p, _ in _DENOMINADORES)
_CIFRA_RE = re.compile(
    rf"(?<![\w.,])({_NUMERO})(?:\s*(?:-|–|a|al|to|y|and)\s*({_NUMERO}))?"
    rf"\s?({_ALTERNATIVAS})((?:/(?:{_ALTERNATIVAS_DE_DENOMINADOR}))*)(?![\w-])",
    re.IGNORECASE,
)
_CANONICA = tuple((re.compile(rf"^(?:{p})$", re.IGNORECASE), c) for p, c in _UNIDADES)
_CANONICA_DE_DENOMINADOR = tuple(
    (re.compile(rf"^(?:{p})$", re.IGNORECASE), c) for p, c in _DENOMINADORES
)
_INTERVALO_DE_CONFIANZA_RE = re.compile(
    r"\s*(?:ci|ic|confidence|confianza|de confianza)\b", re.IGNORECASE
)
_ESPACIOS_RE = re.compile(r"\s+")
_UNIDADES_SIN_MAGNITUD = frozenset({"day", "week", "month", "year", "h", "min", "s"})
_CERO_COMA = "0, "
_CERO_COMA_RE = re.compile(r"(?<![\w.,])0,\s+\Z")
_VENTANA_DE_CERO_COMA = 16
_FIN_DE_FRASE_RE = re.compile(r"[.!?]+(?:\s|$)")
MAX_CARACTERES_DE_FRASE = 300
_FILA_DE_TABLA_RE = re.compile(r"^\s*\|")
_CLAVES_DEL_MOTOR = frozenset({"condition_ref", "condition_refs", "pathway_refs"})


@dataclass(frozen=True)
class Cifra:
    literal: str
    frase: str
    posicion: int
    extremos: tuple[frozenset[tuple[str, str]], ...]

    @property
    def claves(self) -> frozenset[tuple[str, str]]:
        return frozenset().union(*self.extremos)


def _canonico(numero: str) -> str:
    entero, _, decimales = numero.partition(".")
    decimales = decimales.rstrip("0")
    entero = entero.lstrip("0") or "0"
    return f"{entero}.{decimales}" if decimales else entero


def _lecturas(numero: str) -> set[str]:
    if "," in numero and "." in numero:
        decimal = "," if numero.rfind(",") > numero.rfind(".") else "."
        millares = "." if decimal == "," else ","
        sin_millares = numero.replace(millares, "")
        if sin_millares.count(decimal) > 1:
            return {_canonico(sin_millares.replace(decimal, ""))}
        return {_canonico(sin_millares.replace(decimal, "."))}
    separador = "," if "," in numero else "." if "." in numero else None
    if separador is None:
        return {_canonico(numero)}
    partes = numero.split(separador)
    if len(partes) > 2:
        return {_canonico("".join(partes))}
    lecturas = {_canonico(f"{partes[0]}.{partes[1]}")}
    if len(partes[1]) == 3:
        lecturas.add(_canonico("".join(partes)))
    return lecturas


def _lectura_de_cero_coma(fraccion: str) -> str:
    return f"{_CERO_COMA}{fraccion}"


def _inicio_del_cero_coma(texto: str, cifra: re.Match[str], unidad: str) -> int | None:
    if (
        not cifra.group(1).isdigit()
        or cifra.group(2)
        or unidad.split("/")[0] in _UNIDADES_SIN_MAGNITUD
    ):
        return None
    desde = max(0, cifra.start() - _VENTANA_DE_CERO_COMA)
    cero_coma = _CERO_COMA_RE.search(texto, desde, cifra.start())
    return cero_coma.start() if cero_coma else None


def _canonica(literal: str, tabla: tuple[tuple[re.Pattern[str], str], ...]) -> str:
    return next(c for patron, c in tabla if patron.match(literal))


def _unidad(base: str, denominadores: str) -> str:
    partes = [p for p in denominadores.split("/") if p]
    sufijo = "".join(f"/{_canonica(p, _CANONICA_DE_DENOMINADOR)}" for p in partes)
    return _canonica(base, _CANONICA) + sufijo


def _cifras(texto: str) -> Iterator[tuple[int, int, tuple[frozenset[tuple[str, str]], ...]]]:
    for m in _CIFRA_RE.finditer(texto):
        unidad = _unidad(m.group(3), m.group(4))
        if unidad == "%" and _INTERVALO_DE_CONFIANZA_RE.match(texto, m.end()):
            continue
        if (inicio := _inicio_del_cero_coma(texto, m, unidad)) is not None:
            ambigua = frozenset({(_lectura_de_cero_coma(m.group(1)), unidad)})
            yield inicio, m.end(), (ambigua,)
            continue
        yield (
            m.start(),
            m.end(),
            tuple(
                frozenset((lectura, unidad) for lectura in _lecturas(m.group(grupo)))
                for grupo in (1, 2)
                if m.group(grupo)
            ),
        )


def _frase(linea: str, finales: list[int], cifra: tuple[int, int]) -> str:
    anteriores = bisect_right(finales, cifra[0])
    inicio = finales[anteriores - 1] if anteriores else 0
    siguiente = bisect_left(finales, cifra[1])
    fin = finales[siguiente] if siguiente < len(finales) else len(linea)
    if fin - inicio > MAX_CARACTERES_DE_FRASE:
        inicio = max(inicio, cifra[0] - MAX_CARACTERES_DE_FRASE // 2)
        fin = min(fin, inicio + MAX_CARACTERES_DE_FRASE)
        inicio, fin = _palabras_enteras(linea, inicio, fin, cifra)
    return linea[inicio:fin].strip()


def _palabras_enteras(linea: str, inicio: int, fin: int, cifra: tuple[int, int]) -> tuple[int, int]:
    if inicio > 0 and not linea[inicio - 1].isspace():
        espacio = linea.find(" ", inicio, cifra[0])
        inicio = cifra[0] if espacio == -1 else espacio + 1
    if fin < len(linea) and not linea[fin].isspace():
        espacio = linea.rfind(" ", cifra[1], fin)
        fin = cifra[1] if espacio == -1 else espacio
    return inicio, fin


def cifras_del_texto(md: str) -> list[Cifra]:
    vistas: dict[tuple[frozenset[tuple[str, str]], ...], Cifra] = {}
    posicion = 0
    for linea in md.split("\n"):
        if not _FILA_DE_TABLA_RE.match(linea):
            finales = [m.end() for m in _FIN_DE_FRASE_RE.finditer(linea)]
            for inicio, fin, extremos in _cifras(linea):
                if extremos not in vistas:
                    vistas[extremos] = Cifra(
                        linea[inicio:fin],
                        _frase(linea, finales, (inicio, fin)),
                        posicion + inicio,
                        extremos,
                    )
        posicion += len(linea) + 1
    return list(vistas.values())


def _textos(nodo: Any) -> Iterator[str]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if not clave.startswith("_") and clave not in _CLAVES_DEL_MOTOR:
                yield from _textos(valor)
    elif isinstance(nodo, list):
        for valor in nodo:
            yield from _textos(valor)
    elif isinstance(nodo, str | int | float) and not isinstance(nodo, bool):
        yield str(nodo)


def claves_del_json(datos: Any) -> set[tuple[str, str]]:
    texto = _ESPACIOS_RE.sub(" ", "\n".join(_textos(datos)))
    return {clave for *_, extremos in _cifras(texto) for extremo in extremos for clave in extremo}


def cifras_que_faltan(md: str, datos: Any) -> list[Cifra]:
    presentes = claves_del_json(datos)
    return [
        c for c in cifras_del_texto(md) if not all(extremo & presentes for extremo in c.extremos)
    ]