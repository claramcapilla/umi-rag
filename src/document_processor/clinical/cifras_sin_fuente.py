from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .cifras import (
    _CLAVES_DEL_MOTOR,
    _DENOMINADORES,
    _ESPACIOS_RE,
    _FIN_DE_FRASE_RE,
    _INTERVALO_DE_CONFIANZA_RE,
    _NUMERO,
    _UNIDADES,
    _UNIDADES_SIN_MAGNITUD,
    _canonica,
    _canonico,
    _lectura_de_cero_coma,
    _lecturas,
)
from .tablas_markdown import tablas_del_markdown

Par = tuple[str, str]
Ruta = tuple[str | int, ...]
_Tabla = tuple[tuple[re.Pattern[str], str], ...]

_SOLO_EN_MINUSCULA = {"s": r"(?-i:segundos|seconds|sec)", "g": r"(?-i:g)"}
_UNIDADES_DEL_JSON = tuple((_SOLO_EN_MINUSCULA.get(c, p), c) for p, c in _UNIDADES)
_ABREVIATURAS_DEL_DOCUMENTO = (
    (r"wks|sem", "week"),
    (r"mos|mo", "month"),
    (r"yrs", "year"),
    (r"hrs?|hs", "h"),
    (r"(?-i:d)", "day"),
    (r"mgrs?", "mg"),
    (r"gr|gm|G", "g"),
    (r"lg", "ug"),
    (r"(?-i:s)", "s"),
    (r"(?-i:m)", "month"),
)
_UNIDADES_DEL_DOCUMENTO = _UNIDADES_DEL_JSON + _ABREVIATURAS_DEL_DOCUMENTO
_DENOMINADORES_EN_ORDEN = sorted(_DENOMINADORES, key=lambda d: d[1] != "dosis")
_DENOMINADOR = "|".join(f"(?:{p})" for p, _ in _DENOMINADORES_EN_ORDEN)
_DIARIO = "/d"
_SIN_DENOMINADOR = "/dosis"
MAX_CARACTERES_DE_ROTULO = 60
MAX_PROPORCION_DE_RANGO = 10


MAX_DENOMINADORES = 4


def _canonicas(unidades: tuple[tuple[str, str], ...]) -> _Tabla:
    return tuple((re.compile(rf"^(?:{p})$", re.IGNORECASE), c) for p, c in unidades)


def _patron_de_unidad(unidades: tuple[tuple[str, str], ...]) -> str:
    alternativas = "|".join(f"(?:{p})" for p, _ in unidades)
    return (
        rf"(?P<unidad>{alternativas})(?P<den>(?:/(?:{_DENOMINADOR})){{1,{MAX_DENOMINADORES}}}+)?"
        r"(?(den)|(?:(?<=%)|(?![\w-])))"
    )


_CANONICAS_DEL_JSON = _canonicas(_UNIDADES_DEL_JSON)
_CANONICAS_DEL_DOCUMENTO = _canonicas(_UNIDADES_DEL_DOCUMENTO)
_CANONICAS_DE_DENOMINADOR = _canonicas(tuple(_DENOMINADORES_EN_ORDEN))
_NUM = rf"(?>{_NUMERO})"
_UNIDAD_DEL_JSON = _patron_de_unidad(_UNIDADES_DEL_JSON)
_UNIDAD_DEL_DOCUMENTO = _patron_de_unidad(_UNIDADES_DEL_DOCUMENTO)
_ANTES_EN_EL_JSON = r"(?<![\w.,])(?<!\d\+)(?<!\w-)"
_ANTES_EN_EL_DOCUMENTO = r"(?<![\d.,])(?<!\d\+)(?<!\d-)"
_RANGO = rf"(?:\s*(?:-|a|al|to|y|and)\s*({_NUM}))?"
_CONECTOR = (
    r"(?:,|;|-|→|\+|and|y|or|o|to|a|al|vs\.?|versus|frente a|compared (?:with|to))"
    r"(?:\s+(?:el|la|los|las|the))?"
)
_COMPARACION = r"\b(?:vs\.?|versus|frente a|compared (?:with|to))"
_FRECUENCIA = (
    r"(?:twice|thrice|two|three|four|times|veces|dos|tres|cuatro"
    r"|b\.?i\.?d|t\.?i\.?d|q\.?i\.?d|\d+x)"
)
_CADA_DIA = r"(?:daily|al d[ií]a|diari[oa]s?|per day|a day|each day|cada d[ií]a)"
_EDAD = r"\b(?:ages?|aged|edad(?:es)?|older|younger|mayores|menores|niños|children)\b"
_SIN_FIN_DE_FRASE = r"(?:[^.;\n]|(?<=\d)\.(?=\d))"
_ESTADISTICO = r"\((?>mediana|median|media|mean|sd|de|n|%|\s|,|\(|\)){0,30}\)"

_CIFRA_DEL_JSON_RE = re.compile(
    rf"{_ANTES_EN_EL_JSON}({_NUM}){_RANGO}\s?{_UNIDAD_DEL_JSON}", re.IGNORECASE
)
_CIFRA_DEL_DOCUMENTO_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}({_NUM}){_RANGO}\s{{0,3}}{_UNIDAD_DEL_DOCUMENTO}",
    re.IGNORECASE,
)
_LISTA_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}{_NUM}(?:\s*{_CONECTOR}\s*{_NUM})++", re.IGNORECASE
)
_UNIDAD_TRAS_LA_LISTA_RE = re.compile(rf"\s?{_UNIDAD_DEL_DOCUMENTO}", re.IGNORECASE)
_DISPERSION_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}({_NUM})\s*(?:(?:±|\+/-)\s*{_NUM}|\(\s*(?:SD\s*)?{_NUM}\s*\))"
    rf"\s?{_UNIDAD_DEL_DOCUMENTO}",
    re.IGNORECASE,
)
_DOSIS_DIARIA_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}({_NUM})\s?{_UNIDAD_DEL_DOCUMENTO}"
    rf"(?:\s+(?!{_FRECUENCIA}\b)[^\W\d_]+){{1,3}}\s+{_CADA_DIA}\b",
    re.IGNORECASE,
)
_ENTRE_PARENTESIS_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}{_NUM}\s?{_UNIDAD_DEL_DOCUMENTO}{_SIN_FIN_DE_FRASE}{{0,25}}?"
    r"[(\[](?P<cifras>[^)\]\n]{0,120})[)\]]",
    re.IGNORECASE,
)
_TRAS_COMPARAR_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}{_NUM}\s?{_UNIDAD_DEL_DOCUMENTO}{_SIN_FIN_DE_FRASE}{{0,80}}?"
    rf"{_COMPARACION}"
    rf"\s*(?P<cifras>{_NUM})",
    re.IGNORECASE,
)
_ROTULO_RE = re.compile(
    rf"(?<![\w.,/])(?<![\d.,]\s){_UNIDAD_DEL_DOCUMENTO}\)?[^\S\n]*(?:{_ESTADISTICO}[^\S\n]*)?"
    rf"(?P<cifras>(?:{_NUM}(?:[^\S\n]*\([^)\n]{{0,20}}\))?[^\S\n]*(?:[*†‡]|±|\+/-)*[^\S\n]*)+)",
    re.IGNORECASE,
)
_EDAD_RE = re.compile(
    rf"{_EDAD}[^.\n]{{0,30}}?{_ANTES_EN_EL_DOCUMENTO}(?P<cifras>{_NUM})(?![.,]?\d)"
    rf"(?!\s?{_UNIDAD_DEL_DOCUMENTO})",
    re.IGNORECASE,
)
_UNO_DE_CADA_RE = re.compile(
    rf"\b(?:one|1|uno|una)\s+(?:in|out of|de cada)\s+(?P<cifras>{_NUM})(?![\w.,])", re.IGNORECASE
)
_UNIDAD_SUELTA_RE = re.compile(rf"(?<![\w.,/])(?<!\d\s){_UNIDAD_DEL_DOCUMENTO}", re.IGNORECASE)
_NUMERO_RE = re.compile(_NUM)
_NIVEL_DE_CONFIANZA_RE = re.compile(r"\b(?:CI|IC)(?:\s+del?)?\s*$", re.IGNORECASE)
NIVELES_DE_CONFIANZA = frozenset({"90", "95", "99"})
_CIFRA_PROPIA_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}{_NUM}\s?{_UNIDAD_DEL_DOCUMENTO}|\b[np]\s*[=<>≤≥]\s*{_NUM}",
    re.IGNORECASE,
)
_ROTULO_ANTES_DE_RANGO_RE = re.compile(
    r"\b(?:grade|grado|stage|estadio|type|tipo|figure|figura|fig\.?|table|tabla|step|paso"
    r"|phase|fase|level|nivel|cycle|ciclo|day|d[ií]a|week|semana|n|no\.?)\s*$",
    re.IGNORECASE,
)
_DE_CADA_CIEN_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}(?P<cifras>{_NUM})\s+(?:out of|of every|de cada)\s+100(?![\d.,])",
    re.IGNORECASE,
)
_INTERVALO_TRAS_LA_CIFRA_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}{_NUM}\s?{_UNIDAD_DEL_DOCUMENTO}{_SIN_FIN_DE_FRASE}{{0,40}}?"
    rf"\b(?:CI|IC)\b[^\d\n]{{0,12}}?(?P<cifras>{_NUM}\s*(?:to|a|-|,|;)\s*-?{_NUM})",
    re.IGNORECASE,
)
_RANGO_SIN_GUION_RE = re.compile(
    rf"{_ANTES_EN_EL_DOCUMENTO}(?<![\d.,]\s)(?P<inicio>{_NUM}) (?P<fin>{_NUM})"
    rf"{_UNIDAD_DEL_DOCUMENTO}",
    re.IGNORECASE,
)
_CON_LA_UNIDAD_DE_OTRA_CIFRA = (
    _ENTRE_PARENTESIS_RE,
    _TRAS_COMPARAR_RE,
    _ROTULO_RE,
    _INTERVALO_TRAS_LA_CIFRA_RE,
)


def _cero_coma_re(unidades: tuple[tuple[str, str], ...]) -> re.Pattern[str]:
    magnitudes = tuple((p, c) for p, c in unidades if c not in _UNIDADES_SIN_MAGNITUD)
    return re.compile(
        rf"(?<![\w.,])0,\s+(?P<fraccion>\d+)\s?{_patron_de_unidad(magnitudes)}", re.IGNORECASE
    )


_CERO_COMA_DEL_JSON_RE = _cero_coma_re(_UNIDADES_DEL_JSON)
_CERO_COMA_DEL_DOCUMENTO_RE = _cero_coma_re(_UNIDADES_DEL_DOCUMENTO)
_CIFRA_APARTADA = "¤"
_GUIONES_RE = re.compile("[‐-―−]")
_DECIMAL_O_RANGO_RE = (
    (re.compile(r"(?<=\d)[·]|(?<=\d) ́(?=\d)"), "."),
    (re.compile(r"(?<=\d)~(?=\d)"), "-"),
)
_BARRA_RE = re.compile(r"\s*/\s*")
_METRO_CUADRADO_RE = re.compile(r"/m\s+2\b")
_CADA_24_HORAS_RE = re.compile(
    r"(?:\s*/\s*|\s+(?:cada|every|each)\s+|\s*q)24\s*h(?:oras|ours|rs|s)?\b", re.IGNORECASE
)
_POR_RE = re.compile(
    r"\s+(?:per|a|each|por|al|cada)\s+"
    r"(d[ií]as?|days?|weeks?|semanas?|hours?|horas?|kg|m2|m²|dosis|doses?|min)\b(?!\s*altern)",
    re.IGNORECASE,
)
_DIA_A_LA_MENOS_UNO_RE = re.compile(r"(?<=[a-zµ])\s+day(?:⁻¹|-1|(?![\w-])(?!\s*\d))", re.IGNORECASE)
_DIARIO_RE = re.compile(
    r"\s*(?:\s(?:once daily|daily|diari[oa]s?|al d[ií]a|q\.?\s?d\.?|o\.?\s?d\.?"
    r"|once (?:a|per) day|una vez al d[ií]a|d[ií]a(?!\s*\d))"
    r"|\s?1\s?x\s?/\s?(?:days?|d[ií]a|d))(?!\w)",
    re.IGNORECASE,
)
_GUION_ANTES_DE_UNIDAD_RE = re.compile(
    r"(\d)\s*-\s*(?=(?:days?|d|weeks?|wk|months?|mo|years?|yr|hours?|h|min|minutes?"
    r"|mg|g|kg|ml|mm|cm)\b)",
    re.IGNORECASE,
)
_EN_PALABRAS = {
    "dos": "2",
    "two": "2",
    "tres": "3",
    "three": "3",
    "cuatro": "4",
    "four": "4",
    "cinco": "5",
    "five": "5",
    "seis": "6",
    "six": "6",
    "siete": "7",
    "seven": "7",
    "ocho": "8",
    "eight": "8",
    "nueve": "9",
    "nine": "9",
    "diez": "10",
    "ten": "10",
    "eleven": "11",
    "doce": "12",
    "twelve": "12",
    "fourteen": "14",
    "quince": "15",
    "fifteen": "15",
    "veinte": "20",
    "twenty": "20",
    "treinta": "30",
    "thirty": "30",
    "cuarenta": "40",
    "forty": "40",
    "cincuenta": "50",
    "fifty": "50",
    "sesenta": "60",
    "sixty": "60",
}
_UNO = {"un": "1", "una": "1", "uno": "1", "one": "1"}
_TIEMPO = (
    r"(?:d[ií]as?|days?|semanas?|weeks?|mes(?:es)?|months?|a[ñn]os?|years?|horas?|hours?"
    r"|minutos?|minutes?)"
)
_DECENAS = {"twenty": 20, "veinte": 20, "thirty": 30, "treinta": 30, "forty": 40, "cuarenta": 40}
_DECENAS |= {"fifty": 50, "cincuenta": 50, "sixty": 60, "sesenta": 60}
_UNIDADES_EN_PALABRAS = {"one": 1, "uno": 1, "two": 2, "dos": 2, "three": 3, "tres": 3}
_UNIDADES_EN_PALABRAS |= {"four": 4, "cuatro": 4, "five": 5, "cinco": 5, "six": 6, "seis": 6}
_UNIDADES_EN_PALABRAS |= {"seven": 7, "siete": 7, "eight": 8, "ocho": 8, "nine": 9, "nueve": 9}
_COMPUESTO_RE = re.compile(
    rf"\b({'|'.join(_DECENAS)})(?:-|\s+y\s+)({'|'.join(_UNIDADES_EN_PALABRAS)})\b", re.IGNORECASE
)
_EN_PALABRAS_RE = re.compile(rf"\b({'|'.join(_EN_PALABRAS)})\b", re.IGNORECASE)
_UNO_RE = re.compile(
    rf"\b({'|'.join(_UNO)})\b(?=(?:\s*(?:a|to|-)\s*\w+)?[\s-]+{_TIEMPO}\b)", re.IGNORECASE
)
_PALABRA_ENTRE_CIFRA_Y_TIEMPO_RE = re.compile(
    rf"(\d)\s+(?:primer[oa]s|first|[uú]ltim[oa]s|last|next|siguientes)\s+(?={_TIEMPO}\b)",
    re.IGNORECASE,
)
_DE_EDAD_RE = re.compile(r"\s*-\s*old\b", re.IGNORECASE)
_VS_PEGADO_RE = re.compile(r"\bvs\.(?=\d)", re.IGNORECASE)
_PALABRA_PARTIDA_RE = re.compile(r"(?<=[^\W\d_]{2})- (?=[^\W\d_]{2})")
_E_COMO_GUION_RE = re.compile(r"(\d) ?e ?(\d)")
_PUNTO_SEPARADO_RE = re.compile(r"(\d) \.(\d)")
_ESPACIOS_DE_RELLENO_RE = re.compile(r"[^\S\n]{2,}")
_LINEAS_EN_BLANCO_RE = re.compile(r"\n\s*\n\s*")
_SALTO_HTML_RE = re.compile(r"(?:<br\s*/?>)+", re.IGNORECASE)
_ACENTOS_PARTIDOS = (
    ("˜n", "ñ"),
    ("n˜", "ñ"),
    ("ı´", "í"),
    ("´ı", "í"),
    ("a´", "á"),
    ("e´", "é"),
    ("o´", "ó"),
    ("u´", "ú"),
)


def _unidad(base: str, denominadores: str | None, canonicas: _Tabla) -> str:
    partes = [p for p in (denominadores or "").split("/") if p]
    sufijo = "".join(f"/{_canonica(p, _CANONICAS_DE_DENOMINADOR)}" for p in partes)
    return _canonica(base, canonicas) + sufijo


def _normalizar(texto: str, por_dia: bool = True) -> str:
    for patron, sustituto in _DECIMAL_O_RANGO_RE:
        texto = patron.sub(sustituto, texto)
    texto = _GUIONES_RE.sub("-", texto)
    texto = _BARRA_RE.sub("/", texto)
    texto = _METRO_CUADRADO_RE.sub("/m2", texto)
    if por_dia:
        texto = _CADA_24_HORAS_RE.sub(_DIARIO, texto)
        texto = _POR_RE.sub(r"/\1", texto)
        texto = _DIA_A_LA_MENOS_UNO_RE.sub(_DIARIO, texto)
        texto = _DIARIO_RE.sub(_DIARIO, texto)
    return _GUION_ANTES_DE_UNIDAD_RE.sub(r"\1 ", texto)


def _compuesto(m: re.Match[str]) -> str:
    return str(_DECENAS[m.group(1).lower()] + _UNIDADES_EN_PALABRAS[m.group(2).lower()])


def _texto_del_documento(md: str, por_dia: bool) -> str:
    texto = _ESPACIOS_DE_RELLENO_RE.sub(" ", _SALTO_HTML_RE.sub(" ", md))
    texto = _LINEAS_EN_BLANCO_RE.sub("\n\n", texto)
    for partido, entero in _ACENTOS_PARTIDOS:
        texto = texto.replace(partido, entero)
    texto = _COMPUESTO_RE.sub(_compuesto, texto)
    texto = _UNO_RE.sub(lambda m: _UNO[m.group(1).lower()], texto)
    texto = _EN_PALABRAS_RE.sub(lambda m: _EN_PALABRAS[m.group(1).lower()], texto)
    texto = _PALABRA_PARTIDA_RE.sub("", _VS_PEGADO_RE.sub("vs. ", texto))
    texto = _PALABRA_ENTRE_CIFRA_Y_TIEMPO_RE.sub(r"\1 ", texto)
    texto = _DE_EDAD_RE.sub(" old", _normalizar(texto, por_dia))
    texto = _E_COMO_GUION_RE.sub(r"\1-\2", texto)
    return _PUNTO_SEPARADO_RE.sub(r"\1.\2", texto)


def _ceros_coma(
    texto: str, patron: re.Pattern[str], canonicas: _Tabla
) -> list[tuple[re.Match[str], str]]:
    ceros = []
    for m in patron.finditer(texto):
        unidad = _unidad(m.group("unidad"), m.group("den"), canonicas)
        if not (unidad == "%" and _INTERVALO_DE_CONFIANZA_RE.match(texto, m.end())):
            ceros.append((m, unidad))
    return ceros


def _apartar(texto: str, ceros: list[tuple[re.Match[str], str]]) -> str:
    partes = []
    desde = 0
    for m, _ in ceros:
        partes += [texto[desde : m.start()], _CIFRA_APARTADA]
        desde = m.end()
    return "".join(partes) + texto[desde:]


def _apartar_cero_coma(texto: str) -> tuple[str, set[Par]]:
    ceros = _ceros_coma(texto, _CERO_COMA_DEL_DOCUMENTO_RE, _CANONICAS_DEL_DOCUMENTO)
    pares = {(_lectura_de_cero_coma(m.group("fraccion")), unidad) for m, unidad in ceros}
    return _apartar(texto, ceros), pares


def _todos(texto: str, unidad: str) -> set[Par]:
    return {(lectura, unidad) for n in _NUMERO_RE.findall(texto) for lectura in _lecturas(n)}


def _unidad_del_documento(m: re.Match[str]) -> str:
    return _unidad(m.group("unidad"), m.group("den"), _CANONICAS_DEL_DOCUMENTO)


def _cifras_con_unidad(texto: str) -> set[Par]:
    pares: set[Par] = set()
    for m in _CIFRA_DEL_DOCUMENTO_RE.finditer(texto):
        unidad = _unidad_del_documento(m)
        pares |= {
            (lectura, unidad) for g in (1, 2) if m.group(g) for lectura in _lecturas(m.group(g))
        }
    return pares


def _unidad_compartida(texto: str) -> set[Par]:
    pares: set[Par] = set()
    for m in _LISTA_RE.finditer(texto):
        if tras_la_lista := _UNIDAD_TRAS_LA_LISTA_RE.match(texto, m.end()):
            pares |= _todos(m.group(0), _unidad_del_documento(tras_la_lista))
    for m in _DISPERSION_RE.finditer(texto):
        pares |= _todos(m.group(1), _unidad_del_documento(m))
    for m in _DOSIS_DIARIA_RE.finditer(texto):
        pares |= _todos(m.group(1), _unidad_del_documento(m) + _DIARIO)
    for patron in _CON_LA_UNIDAD_DE_OTRA_CIFRA:
        for m in patron.finditer(texto):
            sin_unidad_propia = _CIFRA_PROPIA_RE.sub(" ", m.group("cifras"))
            pares |= _todos(sin_unidad_propia, _unidad_del_documento(m))
    return pares


def _unidad_implicita(texto: str) -> set[Par]:
    pares: set[Par] = set()
    for m in _EDAD_RE.finditer(texto):
        pares |= _todos(m.group("cifras"), "year")
    for m in _UNO_DE_CADA_RE.finditer(texto):
        pares |= _porcentajes_de_uno_entre(m.group("cifras"))
    for m in _DE_CADA_CIEN_RE.finditer(texto):
        pares |= _todos(m.group("cifras"), "%")
    for m in _RANGO_SIN_GUION_RE.finditer(texto):
        rotulo = _ROTULO_ANTES_DE_RANGO_RE.search(texto, max(0, m.start() - 12), m.start())
        if _rango_plausible(m.group("inicio"), m.group("fin")) and not rotulo:
            pares |= _todos(f"{m.group('inicio')} {m.group('fin')}", _unidad_del_documento(m))
    return pares


def _menor(inicio: str, fin: str) -> bool:
    return min(map(float, _lecturas(inicio))) < max(map(float, _lecturas(fin)))


def _rango_plausible(inicio: str, fin: str) -> bool:
    return _menor(inicio, fin) and min(map(float, _lecturas(fin))) <= MAX_PROPORCION_DE_RANGO * max(
        map(float, _lecturas(inicio))
    )


def _mucho_mayor(numero: str, otro: str) -> bool:
    return min(map(float, _lecturas(numero))) > MAX_PROPORCION_DE_RANGO * max(
        map(float, _lecturas(otro))
    )


def _porcentajes_de_uno_entre(numero: str) -> set[Par]:
    divisores = [float(lectura) for lectura in _lecturas(numero) if float(lectura) > 0]
    return {
        (_canonico(f"{100 / divisor:.{decimales}f}"), "%")
        for divisor in divisores
        for decimales in (0, 1)
    }


def _unidades_del_rotulo(rotulo: str) -> set[str]:
    if len(rotulo) > MAX_CARACTERES_DE_ROTULO:
        return set()
    return {_unidad_del_documento(m) for m in _UNIDAD_SUELTA_RE.finditer(rotulo)}


def _con_unidades(celdas: str, unidades: set[str]) -> set[Par]:
    return {par for unidad in unidades for par in _todos(celdas, unidad)}


def _unidades_de_tabla(texto: str) -> set[Par]:
    pares: set[Par] = set()
    for tabla in tablas_del_markdown(texto):
        for j, cabecera in enumerate(tabla.columnas):
            columna = " ".join(fila[j] for fila in tabla.filas if j < len(fila))
            pares |= _con_unidades(columna, _unidades_del_rotulo(cabecera))
        de_la_seccion: set[str] = set()
        for rotulo, *celdas in tabla.filas:
            if not any(celdas):
                de_la_seccion = _unidades_del_rotulo(rotulo)
                continue
            fila = " ".join(celdas)
            pares |= _con_unidades(fila, _unidades_del_rotulo(rotulo) | de_la_seccion)
    return pares


def _pares_del_documento(md: str) -> set[Par]:
    pares: set[Par] = set()
    for por_dia in (True, False):
        texto, pares_de_cero_coma = _apartar_cero_coma(_texto_del_documento(md, por_dia))
        pares |= pares_de_cero_coma
        pares |= _cifras_con_unidad(texto) | _unidad_compartida(texto) | _unidad_implicita(texto)
        pares |= _unidades_de_tabla(texto)
    return pares


def _con_y_sin_dosis(unidad: str) -> set[str]:
    return {unidad, unidad.replace(_SIN_DENOMINADOR, "")}


def _cifras_del_texto(texto: str) -> Iterator[tuple[str, tuple[frozenset[Par], ...]]]:
    texto = _normalizar(_ESPACIOS_RE.sub(" ", texto))
    ceros = _ceros_coma(texto, _CERO_COMA_DEL_JSON_RE, _CANONICAS_DEL_JSON)
    for m, unidad in ceros:
        lectura = _lectura_de_cero_coma(m.group("fraccion"))
        yield m.group(0), (frozenset((lectura, u) for u in _con_y_sin_dosis(unidad)),)
    texto = _apartar(texto, ceros)
    for m in _CIFRA_DEL_JSON_RE.finditer(texto):
        unidad = _unidad(m.group("unidad"), m.group("den"), _CANONICAS_DEL_JSON)
        if unidad == "%" and (
            _INTERVALO_DE_CONFIANZA_RE.match(texto, m.end())
            or m.group(1) in NIVELES_DE_CONFIANZA
            and _NIVEL_DE_CONFIANZA_RE.search(texto, max(0, m.start() - 12), m.start())
        ):
            continue
        unidades = _con_y_sin_dosis(unidad)
        extremos = [m.group(g) for g in (1, 2) if m.group(g)]
        if len(extremos) == 2 and _mucho_mayor(*extremos):
            extremos = extremos[1:]
        yield (
            m.group(0),
            tuple(
                frozenset((lectura, u) for lectura in _lecturas(extremo) for u in unidades)
                for extremo in extremos
            ),
        )


def _hojas(nodo: Any, ruta: Ruta = ()) -> Iterator[tuple[Ruta, str]]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if not clave.startswith("_") and clave not in _CLAVES_DEL_MOTOR:
                yield from _hojas(valor, (*ruta, clave))
    elif isinstance(nodo, list):
        for i, valor in enumerate(nodo):
            yield from _hojas(valor, (*ruta, i))
    elif isinstance(nodo, str):
        yield ruta, nodo


def _frases(texto: str) -> list[str]:
    frases = []
    inicio = 0
    for m in _FIN_DE_FRASE_RE.finditer(texto):
        frases.append(texto[inicio : m.end()])
        inicio = m.end()
    if inicio < len(texto):
        frases.append(texto[inicio:])
    return frases


@dataclass(frozen=True)
class CampoSinFuente:
    ruta: Ruta
    cifras: tuple[str, ...]


class RevisorDeCifras:
    def __init__(self, md: str) -> None:
        self._del_documento = _pares_del_documento(md)

    def sin_fuente(self, texto: str) -> list[str]:
        return sorted(
            {
                literal
                for literal, extremos in _cifras_del_texto(texto)
                if not all(extremo & self._del_documento for extremo in extremos)
            }
        )

    def campos(self, datos: Any) -> list[CampoSinFuente]:
        return [
            CampoSinFuente(ruta, tuple(cifras))
            for ruta, texto in _hojas(datos)
            if (cifras := self.sin_fuente(texto))
        ]

    def sin_frases_sin_fuente(self, texto: str) -> str | None:
        resto = "".join(f for f in _frases(texto) if not self.sin_fuente(f)).strip()
        return resto if resto and not self.sin_fuente(resto) else None


def cifras_sin_fuente(md: str, datos: Any) -> list[str]:
    return sorted({c for campo in RevisorDeCifras(md).campos(datos) for c in campo.cifras})