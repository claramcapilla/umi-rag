"""Limpieza y optimización de Markdown antes de enviarlo al LLM."""

from __future__ import annotations

import html as _html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, overload

from src.document_processor.config import HEADING_RE as _HEADING_RE

_LIGATURE_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}

_UNI_ESCAPE_RE = re.compile(r"/uni([0-9A-Fa-f]{4})")


def fix_ligatures(text: str) -> str:
    """Reemplaza ligaduras Unicode y /uniXXXX."""
    for lig, replacement in _LIGATURE_MAP.items():
        text = text.replace(lig, replacement)

    def _replace_uni(m: re.Match[str]) -> str:
        code = int(m.group(1), 16)
        char = chr(code)
        return _LIGATURE_MAP.get(char, char)

    text = _UNI_ESCAPE_RE.sub(_replace_uni, text)
    return text


_WASTE_HEADING_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^(\d+(\.\d+)*\.?\s+)?"
            r"(references?|key\s+references|"
            r"referencias(\s+bibliogr[aá]ficas)?|bibliography|"
            r"bibliograf[ií]a|"
            r"literatur|literatura|refs?):?$",
            re.IGNORECASE,
        ),
        "Referencias",
    ),
    (
        re.compile(
            r"^\d+[\.\d]*\s*\.?\s*bibliograf[ií]a$",
            re.IGNORECASE,
        ),
        "Bibliografía",
    ),
    (
        re.compile(
            r"(^(\d+(\.\d+)*\.?\s+)?(financial\s+)?disclosures?"
            r"(\s+(statements?|of\s+interests?))?:?$|"
            r"conflicts?\s+(of\s+)?interests?|"
            r"interessenkonflik|"
            r"conflictos?\s+de\s+inter[eé]s|"
            r"declaraci[oó]n.{0,20}inter[eé]s)",
            re.IGNORECASE,
        ),
        "Conflictos de Interés",
    ),
    (
        re.compile(
            r"^\W*(anschriften|author.?s?\s*address|"
            r"correspondence|"
            r"contact\s+information|"
            r"direcci[oó]n.*(autor|contact))",
            re.IGNORECASE,
        ),
        "Direcciones de Autores",
    ),
    (
        re.compile(
            r"^(funding|acknowledge?ments?|"
            r"financial\s+support|"
            r"financiaci[oó]n|agradecimientos|"
            r"qui[eé]nes\s+nos\s+apoyan)$",
            re.IGNORECASE,
        ),
        "Financiación",
    ),
    (
        re.compile(
            r"^(contenido|table\s+of\s+contents|"
            r"inhaltsverzeichnis|[ií]ndice)$",
            re.IGNORECASE,
        ),
        "Tabla de Contenidos",
    ),
    (
        re.compile(
            r"^(coordinadores|coordinaci[oó]n|"
            r"autores|authors|contributors|"
            r"colaboraciones|colaboradores|"
            r"herausgeber)(?!.*conclusi)(\s|$)",
            re.IGNORECASE,
        ),
        "Autores",
    ),
    (
        re.compile(
            r"^(acerca\s+de\s+nccn|"
            r"about\s+nccn|"
            r"nccn\s+guidelines\s+for\s+patients"
            r"\s+cuenta|"
            r"centros\s+oncol[oó]gicos\s+de\s+nccn)",
            re.IGNORECASE,
        ),
        "Boilerplate NCCN",
    ),
    (
        re.compile(
            r"^(miembros\s+del\s+panel|panel\s+members)",
            re.IGNORECASE,
        ),
        "Panel",
    ),
    (
        re.compile(
            r"^otras\s+fuentes\s+de\s+informaci",
            re.IGNORECASE,
        ),
        "Boilerplate",
    ),
    (
        re.compile(
            r"^datos\s+de\s+contacto$",
            re.IGNORECASE,
        ),
        "Contacto",
    ),
    (
        re.compile(
            r"^(acknowledgments?|"
            r"author\s+declarations?|"
            r"author\s+contributions?|"
            r"data\s+availability|"
            r"data\s+access\s+statement|"
            r"availability\s+of\s+data)$",
            re.IGNORECASE,
        ),
        "Declaraciones",
    ),
    (
        re.compile(
            r"articles?\s+you\s+may\s+be\s+" r"interested\s+in",
            re.IGNORECASE,
        ),
        "Boilerplate",
    ),
    (
        re.compile(
            r"^(titular\s+de\s+la\s+autorizaci|"
            r"responsable\s+de\s+la\s+fabricaci)",
            re.IGNORECASE,
        ),
        "Fabricante",
    ),
]

_ENCABEZADO_DE_PERSONA = r"^Prof\.|^Dr\.|^PD\s"
_PERSONA_RE = re.compile(_ENCABEZADO_DE_PERSONA, re.IGNORECASE)
_CONTINUACION_DE_BIBLIOGRAFIA = (
    r"^Annals\s+of\s+Oncol|"
    r"^A\.\s*J\.\s*M\.\s*Ferreri|"
    r"^[A-Z]\.\s+[A-Z][\w]+\s+et\s+al|"
    r"^\d{4}$|"
    r"^Journal\s+Pre-proof$|"
    r"^NCCN$"
)
_CONTINUACION_DE_BIBLIOGRAFIA_RE = re.compile(_CONTINUACION_DE_BIBLIOGRAFIA, re.IGNORECASE)
_WASTE_CONTINUATION_RE = re.compile(
    rf"({_ENCABEZADO_DE_PERSONA}|"
    rf"{_CONTINUACION_DE_BIBLIOGRAFIA}|"
    r"Cancer (Center|Institute)|"
    r"Medical Center|"
    r"Comprehensive Cancer)",
    re.IGNORECASE,
)

_RUNNING_HEADER_MIN_REPEATS = 3
_MARCA_DE_MOBILIARIO_RE = re.compile(
    r"©|\bcopyright\b|downloaded\s*from|all\s+rights\s+reserved|https?://|\bwww\.|\bdoi\b",
    re.IGNORECASE,
)
_MAX_CARACTERES_DE_MOBILIARIO = 150
_CONTINUACION_DE_FRASE_RE = re.compile(r"[a-zß-ÿ]{2,}")
_TOKEN_PEGADO_RE = re.compile(r"[^\W\d_]{25,}|\b[A-Z]{17,}")
_PALABRA_RE = re.compile(r"[^\W\d_]+")
_CONTENIDO_CLINICO_RE = re.compile(
    r"[%;]|\d\s*(?:mg|µg|mcg|ng|g|kg|ml|dl|l|mm|cm|mmhg|mmol|µmol|ui|iu|u|h|min|"
    r"d[ií]as?|days?|horas?|hours?|semanas?|weeks?|meses|months?|años|years?)\b",
    re.IGNORECASE,
)

_ETIQUETAS_DE_BIBLIOGRAFIA = frozenset({"Referencias", "Bibliografía"})
_ETIQUETA_DE_INDICE = "Tabla de Contenidos"
_ETIQUETA_DE_AUTORES = "Autores"
_ETIQUETAS_DE_PERSONAS = frozenset(
    {
        _ETIQUETA_DE_AUTORES,
        "Direcciones de Autores",
        "Contacto",
        "Panel",
        "Fabricante",
        "Distribuidores EU",
    }
)
_TEXTO_DE_DECLARACION_RE = re.compile(
    r"\breceiv\w*\s+(?:\w+\s+){0,4}?(?:fees?|honorari\w*|grants?|funding|support|payments?)\b|"
    r"\bhonorari|\bno\s+(?:\w+\s+)?(?:conflicts?|competing|financial|funding|disclosures?)\b|"
    r"\b(?:conflicts?\s+of\s+interests?|competing\s+interests?|nothing\s+to\s+disclose|"
    r"(?:authors?|we|I)\s+(?:\w+\s+){0,2}?(?:declares?|declared|disclosed?|thanks?|acknowledges?)|"
    r"(?:we|authors?|are|is)\s+grateful|funded\s+by|sponsored\s+by|"
    r"funding\s+(?:from|for|source)|grants?\s+(?:no|number|from)|advisory\s+boards?|"
    r"speakers?'?\s+(?:bureau|fees)|consult\w*\s+fees|employee\s+of|(?:holds?|owns?)\s+stock|"
    r"royalt\w*|data\s+availab\w*)\b|"
    r"\b(?:recibi[oó]\s+(?:\w+\s+){0,3}?(?:honorarios|financiaci[oó]n|becas?|ayudas?|pagos?)|"
    r"declara(?:n|mos)?\s+(?:no|que)|conflictos?\s+de\s+inter[eé]s(?:es)?|"
    r"agradec(?:emos|en|imientos?|er)|financiad[oa]s?\s+por|sin\s+financiaci[oó]n|"
    r"por\s+sus?\s+(?:revisi[oó]n|colaboraci[oó]n|ayuda|apoyo|comentarios|sugerencias)|"
    r"(?:dar\s+las\s+)?gracias\s+a|"
    r"disponibilidad\s+de\s+(?:los\s+)?datos)\b",
    re.IGNORECASE,
)
_DATO_DE_CONTACTO_RE = re.compile(
    r"(?i:\b(?:tel|tlf|tfno|tel[eé]fono|phone|fax|m[oó]vil|orcid)\b)|\bC/\s*[A-ZÀ-Ý]|"
    r"(?<![\d.,])\d{2,3}(?:[\s.]\d{2,3}){2,3}(?![\d.,])|\b\d{5}\s+[A-ZÀ-Ý][a-zà-ÿ]"
)
_CREDENCIAL_RE = re.compile(
    r"\b(?:MD|PhD|RN|MSc|MPH|FRCOG|FACOG|FRCS|PharmD)\b|"
    r"(?i:\b(?:consultant|professor|profesora?|catedr[aá]tic\w*)\b)|"
    r"(?i:^(?:dra?|prof)\.?\s)|\b(?:Dra?|Prof)\.\s"
)
_BIOGRAFIA_RE = re.compile(
    r"\b(?:(?:she|he)\s+(?:is|was|has|led|leads|chairs|works|trained|holds)|trabaja\s+en|"
    r"es\s+especialista|dirige|jef[ae]\s+de)\b",
    re.IGNORECASE,
)
_ENCABEZADO_DE_PERSONAS_RE = re.compile(
    r"^(?:(?:grupo|group)\s+(?:de\s+)?(?:trabajo|elaboraci[oó]n|redacci[oó]n|working|"
    r"development|guideline)|revisor\w*|reviewers?|comit[eé]|committee|coordina\w*|autor\w*|"
    r"authors?|colabora\w*|contributors?|panel|miembros?|members?|editor\w*|redac\w*|asesor\w*|"
    r"expert\s+(?:panel|group|reviewers?))\b",
    re.IGNORECASE,
)
_MIN_PALABRAS_DE_TEXTO = 4
_SEPARADOR_DE_NOMBRES_RE = re.compile(r",|\s(?:y|and|&)\s")
_CONECTORES_DE_NOMBRE = frozenset(
    {"de", "del", "la", "las", "los", "y", "i", "van", "von", "da", "dos", "di", "and", "&"}
)
_MAX_CARACTERES_DE_NOMBRES = 300
_APELLIDO = r"[A-ZÀ-Ý][\w'’\-]+"
_INICIO_DE_CITA_RE = re.compile(rf"^[\W\d]*{_APELLIDO}\s+[A-Z]{{1,3}}(?:[.,]|\s*$)")
_LISTA_DE_AUTORES_RE = re.compile(
    rf"^[\W\d]*(?:[A-Z]{{1,3}},\s*)?(?:{_APELLIDO}\s+[A-Z]{{1,3}},\s*){{3,}}"
)
_PRUEBA_DE_CITA_RE = re.compile(r"\b(?:19|20)\d\d\b|\bet\s+al\b")
_MARCA_DE_CITA_RE = re.compile(
    r"\bdoi\b|\bPMID\b|\[PubMed|\bISBN\b|\bArt\.\s*No\b|"
    r"\b(?:19|20)\d\d[a-z]?(?:\s+[A-Z][a-z]{2})?\s*;\s*\d+|"
    r"\b(?:19|20)\d\d[a-z]?[.,]\s*\d+\s*(?:\(\d+\))?\s*:\s*\d+|"
    r"\b(?:available|accessed|disponible|consultado)\b[^\n]{0,40}https?://",
    re.IGNORECASE,
)
_MAX_CARACTERES_DE_CITA = 300
_AUTORES_CON_COMA_RE = re.compile(rf"^[\W\d]*{_APELLIDO},\s*(?:[A-Z]\.\s*-?){{1,4}}[;,]")
_MARCA_DE_REFERENCIA_RE = re.compile(
    r"\[(?:CrossRef|PubMed|Google Scholar|Green Version|PubMed Central)\]|\bPMID\b|"
    r"\bdoi:?\s*10\.\d|"
    r"\b(?:19|20)\d\d[a-z]?\s*;\s*\d+\s*(?:\(\d+\))?\s*:\s*[A-Za-z]?\d+|"
    r"\b(?:19|20)\d\d[a-z]?\s*,\s*\d+\s*(?:\(\d+\))?\s*,\s*[A-Za-z]?\d+\s*[–-]\s*\d+",
    re.IGNORECASE,
)
_MAX_CARACTERES_DE_REFERENCIA = 600
_MAX_CARACTERES_TRAS_LA_MARCA = 60
_AUTOR_Y_SIGUIENTE_RE = re.compile(rf"^[\W\d]*{_APELLIDO}\s+[A-Z]{{1,3}}\s+(?:et\s+al|and|y|&)\b")
_COLA_DE_REFERENCIA_RE = re.compile(
    r"^[^\n]{0,60}?\b(?:19|20)\d\d[a-z]?\s*;\s*\d+\s*(?:\(\d+\))?\s*:\s*[A-Za-z]?\d+(?:[–-]\w+)?\.?\s*$"
)
_DOI_SOLO_RE = re.compile(r"^[\W\d]*doi:?\s*10\.\d{3,}/\s*\S+\s*$", re.IGNORECASE)
_MAX_CARACTERES_DE_COLA = 80
_ENTRADA_DE_INDICE_RE = re.compile(r"^.{1,200}?(?:\.{3,}|…+|\t)\s*(?:\d{1,4}|[ivxlc]{1,6})$")
_FIN_DE_FRASE = ".!?:;\"')]}»"

_EU_COUNTRIES = {
    "België",
    "Belgique",
    "Belgien",
    "България",
    "Česká republika",
    "Danmark",
    "Deutschland",
    "Eesti",
    "Ελλάδα",
    "España",
    "France",
    "Hrvatska",
    "Ireland",
    "Ísland",
    "Italia",
    "Κύπρος",
    "Latvija",
    "Lietuva",
    "Luxembourg",
    "Luxemburg",
    "Luxembourg/Luxemburg",
    "Magyarország",
    "Malta",
    "Nederland",
    "Norge",
    "Österreich",
    "Polska",
    "Portugal",
    "România",
    "Slovenija",
    "Slovenská republika",
    "Suomi/Finland",
    "Suomi",
    "Finland",
    "Sverige",
    "United Kingdom",
}

_ENCODING_ARTIFACTS = {
    "/C15": "",
    "/C3": "",
    "/C21": "≥",
    "/C20": "≤",
    "/C6": "±",
}

_ENCODING_RE = re.compile(r"/C\d{1,3}")


@dataclass
class CleaningStats:
    """Estadísticas de la limpieza."""

    original_chars: int = 0
    cleaned_chars: int = 0
    sections_removed: list[str] = field(default_factory=list)
    running_headers_removed: int = 0

    @property
    def reduction_pct(self) -> float:
        if self.original_chars == 0:
            return 0.0
        saved = self.original_chars - self.cleaned_chars
        return (saved / self.original_chars) * 100


def _is_waste_heading(heading_text: str) -> str | None:
    """Devuelve label si es waste, None si no."""
    text = heading_text.strip()
    for pattern, label in _WASTE_HEADING_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _is_eu_country_heading(heading_text: str) -> bool:
    """True si el heading es un país EU/EEA."""
    return heading_text.strip() in _EU_COUNTRIES


def _is_waste_continuation(heading_text: str) -> bool:
    """True si es un sub-heading que sigue siendo waste.

    Ej: "Prof. Dr. med. Christian Buske" bajo
    "Anschriften der Verfasser", o "A. J. M. Ferreri
    et al." bajo REFERENCES.
    """
    return bool(_WASTE_CONTINUATION_RE.search(heading_text.strip()))


def _detect_running_headers(
    lines: list[str],
) -> tuple[set[str], set[str]]:
    """Detecta headings y texto plano que se repiten.

    Returns:
        (heading_texts, plain_texts) — sets de textos
        repetidos ≥N veces.
    """
    heading_counts: dict[str, int] = {}
    text_counts: dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        m = _HEADING_RE.match(line)
        if m:
            text = m.group(2).strip()
            heading_counts[text] = heading_counts.get(text, 0) + 1
        elif (
            len(stripped) > 20
            and not stripped.startswith("|")
            and not stripped.startswith("- ")
            and not stripped.startswith("* ")
            and not stripped.startswith("> ")
            and not stripped.startswith("```")
        ):
            text_counts[stripped] = text_counts.get(stripped, 0) + 1

    headings = {t for t, c in heading_counts.items() if c >= _RUNNING_HEADER_MIN_REPEATS}
    texts = {t for t, c in text_counts.items() if c >= _RUNNING_HEADER_MIN_REPEATS}
    return headings, texts


@dataclass
class _Descarte:
    etiqueta: str
    nivel: int


def _es_cita(texto: str) -> bool:
    return bool(
        (_INICIO_DE_CITA_RE.match(texto) and _PRUEBA_DE_CITA_RE.search(texto))
        or _LISTA_DE_AUTORES_RE.match(texto)
        or (len(texto) <= _MAX_CARACTERES_DE_CITA and _MARCA_DE_CITA_RE.search(texto))
    )


def _es_referencia_suelta(linea: str) -> bool:
    texto = linea.strip()
    if texto.startswith("|"):
        return False
    if _DOI_SOLO_RE.match(texto):
        return True
    if len(texto) <= _MAX_CARACTERES_DE_COLA and _COLA_DE_REFERENCIA_RE.match(texto):
        return True
    empieza_por_autores = bool(
        _INICIO_DE_CITA_RE.match(texto)
        or _LISTA_DE_AUTORES_RE.match(texto)
        or _AUTORES_CON_COMA_RE.match(texto)
        or _AUTOR_Y_SIGUIENTE_RE.match(texto)
    )
    if not empieza_por_autores or len(texto) > _MAX_CARACTERES_DE_REFERENCIA:
        return False
    marcas = list(_MARCA_DE_REFERENCIA_RE.finditer(texto))
    return bool(marcas) and len(texto) - marcas[-1].end() <= _MAX_CARACTERES_TRAS_LA_MARCA


def _remove_stray_references(md_text: str) -> str:
    return "\n".join(linea for linea in md_text.split("\n") if not _es_referencia_suelta(linea))


_INICIO_NUMERADO_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[\d{1,3}\]|\d{1,3}\.)\s+"
    r"(?:[A-Z][\w'’-]+,?\s+(?:[A-Z]{1,3}\b|[A-Z]\.)|(?:[A-Z]\.\s?){1,3}[A-Z][\w'’-]+)"
)
_SENAL_DE_REFERENCIA_RE = re.compile(
    r"\bet al\b|\(\d{4}\)|\b(?:19|20)\d{2}\s*[;:,.]|doi|https?://", re.IGNORECASE
)
_MARCA_DE_REVISTA_RE = re.compile(
    r"\b(?:19|20)\d{2}[a-z]?\s*(?:;|\.\s*\d{1,4}\s*:)\s*\d|\bdoi\b|\bPMID\b|https?://",
    re.IGNORECASE,
)
_AUTOR_Y_ANIO_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:(?:van|von|de|da|di|del|der|den|du|la|le|dos|das)\s){0,2}"
    r"(?:[A-Z][\w'’-]{1,40}(?:\s[A-Z][\w'’-]{1,40}){0,2},\s(?:[A-Z]\.\s?-?){1,4}"
    r"|[A-Z][^\n(]{2,120}\.\s(?=\((?:19|20)\d{2}))"
    r"[^\n]{0,400}?\((?:19|20)\d{2}[a-z]?\)\."
)
_PAGINAS_RE = re.compile(r"\d+\s*\(\d+\)\s*:?\s*\d+|\d+\s*[:–-]\s*\d+\s*\.?\s*$|PMID|PMCID")
_ENCABEZADO_NUMERADO_RE = re.compile(r"^\s*(?:[-*]\s*)?\d+(?:\.\d+)+\.?\s+\S")
_MAX_CONTINUACION = 300
_MAX_FRAGMENTO_DE_REVISTA = 80
_MAX_SEPARACION = 3
_MIN_REFERENCIAS_SEGUIDAS = 3


def _es_continuacion(linea: str) -> bool:
    limpia = linea.strip()
    if (
        not limpia
        or len(limpia) > _MAX_CONTINUACION
        or limpia.startswith(("#", "|"))
        or _ENCABEZADO_NUMERADO_RE.match(limpia)
    ):
        return False
    return bool(
        _SENAL_DE_REFERENCIA_RE.search(limpia)
        or _PAGINAS_RE.search(limpia)
        or (len(limpia) <= _MAX_FRAGMENTO_DE_REVISTA and limpia.endswith("."))
    )


def _corta_la_racha(lineas: list[str], anterior: int, actual: int) -> bool:
    if actual - anterior > _MAX_SEPARACION + 1:
        return True
    return not all(
        not lineas[j].strip() or _es_continuacion(lineas[j]) for j in range(anterior + 1, actual)
    )


def _lineas_de_la_racha(lineas: list[str], racha: list[int]) -> set[int]:
    if len(racha) < _MIN_REFERENCIAS_SEGUIDAS:
        return set()
    quitar = set(range(racha[0], racha[-1] + 1))
    siguiente = racha[-1] + 1
    while siguiente < len(lineas) and (
        not lineas[siguiente].strip()
        or (
            _es_continuacion(lineas[siguiente])
            and _SENAL_DE_REFERENCIA_RE.search(lineas[siguiente])
        )
    ):
        quitar.add(siguiente)
        siguiente += 1
    return quitar


def _remove_reference_runs(md_text: str) -> str:
    lineas = md_text.split("\n")
    inicios = [
        i
        for i, linea in enumerate(lineas)
        if _AUTOR_Y_ANIO_RE.match(linea)
        or (
            _INICIO_NUMERADO_RE.match(linea)
            and _MARCA_DE_REVISTA_RE.search(" ".join(lineas[i : i + 2]))
        )
    ]
    quitar: set[int] = set()
    racha: list[int] = []
    for i in inicios:
        if racha and _corta_la_racha(lineas, racha[-1], i):
            quitar |= _lineas_de_la_racha(lineas, racha)
            racha = []
        racha.append(i)
    quitar |= _lineas_de_la_racha(lineas, racha)
    return "\n".join(linea for i, linea in enumerate(lineas) if i not in quitar)


_FORMULA_PARTIDA_RE = re.compile(r"\b(cm|mm) ?H[ \t]{0,3}\n\s{0,6}(?:2\s{0,6})?O\b")
_IGUAL_COMO_CUARTO_RE = re.compile(r"\b[pPnN] ?¼ ?\d")
_CUARTO_COMO_IGUAL_RE = re.compile(r"(?<!\d)¼")
_THORN_COMO_MAS_RE = re.compile(r"(?<![a-zà-ÿ])þ|þ(?![a-zà-ÿ])")


def _unir_formulas_partidas(md_text: str) -> str:
    return _FORMULA_PARTIDA_RE.sub(lambda m: f"{m.group(1)} H2O", md_text)


def _arreglar_glifos(md_text: str) -> str:
    if _IGUAL_COMO_CUARTO_RE.search(md_text):
        md_text = _CUARTO_COMO_IGUAL_RE.sub("=", md_text)
    return _THORN_COMO_MAS_RE.sub("+", md_text)


def _es_lista_de_nombres(texto: str) -> bool:
    palabras = texto.replace(",", " ").rstrip(".").split()
    nombres = [palabra for palabra in palabras if palabra[:1].isupper()]
    return (
        len(texto) <= _MAX_CARACTERES_DE_NOMBRES
        and len(nombres) >= 2
        and not any(caracter.isdigit() for caracter in texto)
        and all(palabra[:1].isupper() or palabra in _CONECTORES_DE_NOMBRE for palabra in palabras)
    )


def _es_dato_de_persona(texto: str) -> bool:
    if _CREDENCIAL_RE.search(texto) or _es_lista_de_nombres(texto):
        return True
    return not _CONTENIDO_CLINICO_RE.search(texto) and bool(
        _DATO_DE_CONTACTO_RE.search(texto) or _BIOGRAFIA_RE.search(texto)
    )


def _parece_texto(texto: str) -> bool:
    if _CONTENIDO_CLINICO_RE.search(texto):
        return True
    palabras = _PALABRA_RE.findall(texto)
    en_minuscula = sum(1 for palabra in palabras if palabra.islower())
    return len(palabras) >= _MIN_PALABRAS_DE_TEXTO and en_minuscula * 2 >= len(palabras)


def _es_bibliografia(descarte: _Descarte) -> bool:
    return descarte.etiqueta in _ETIQUETAS_DE_BIBLIOGRAFIA


def _es_desecho(texto: str, descarte: _Descarte) -> bool:
    if _es_bibliografia(descarte):
        return _es_cita(texto)
    if descarte.etiqueta == _ETIQUETA_DE_INDICE:
        return bool(_ENTRADA_DE_INDICE_RE.match(texto))
    if descarte.etiqueta in _ETIQUETAS_DE_PERSONAS:
        return _es_dato_de_persona(texto) or not _parece_texto(texto)
    if _TEXTO_DE_DECLARACION_RE.search(texto):
        return True
    if _CONTENIDO_CLINICO_RE.search(texto):
        return False
    return bool(_DATO_DE_CONTACTO_RE.search(texto)) or (
        _es_lista_de_nombres(texto) and bool(_SEPARADOR_DE_NOMBRES_RE.search(texto))
    )


def _sigue_dentro_de_la_seccion_descartada(
    heading_text: str,
    level: int,
    descarte: _Descarte,
) -> bool:
    """Un heading que no cierra la sección que se está descartando.

    Un autor individual, un running header de revista o un país del EEE (listas de
    distribuidores) son subsecciones de lo mismo aunque estén al mismo nivel.
    """
    if _es_bibliografia(descarte):
        return bool(
            _es_cita(heading_text)
            or _PERSONA_RE.search(heading_text)
            or _CONTINUACION_DE_BIBLIOGRAFIA_RE.search(heading_text)
        )
    if _is_waste_continuation(heading_text) or _is_eu_country_heading(heading_text):
        return True
    if level <= descarte.nivel or descarte.etiqueta not in _ETIQUETAS_DE_PERSONAS:
        return False
    return bool(_ENCABEZADO_DE_PERSONAS_RE.search(heading_text)) or _es_dato_de_persona(
        heading_text
    )


def _etiqueta_de_seccion_descartable(heading_text: str) -> str | None:
    """Etiqueta con la que registrar la sección descartada, o `None` si se conserva."""
    etiqueta = _is_waste_heading(heading_text)
    if etiqueta:
        return etiqueta
    if _is_eu_country_heading(heading_text):
        return "Distribuidores EU"
    return None


def _descarte_tras_encabezado(
    heading: re.Match[str],
    descarte: _Descarte | None,
    stats: CleaningStats,
) -> tuple[_Descarte | None, bool]:
    level = len(heading.group(1))
    heading_text = heading.group(2).strip()
    if descarte and _sigue_dentro_de_la_seccion_descartada(heading_text, level, descarte):
        if _es_bibliografia(descarte) and _PERSONA_RE.search(heading_text):
            return _Descarte(_ETIQUETA_DE_AUTORES, descarte.nivel), True
        if _es_bibliografia(descarte) or _es_dato_de_persona(heading_text):
            return descarte, True
        if not _etiqueta_de_seccion_descartable(heading_text):
            return descarte, False
    etiqueta = _etiqueta_de_seccion_descartable(heading_text)
    if not etiqueta:
        return None, False
    if etiqueta not in stats.sections_removed:
        stats.sections_removed.append(etiqueta)
    return _Descarte(etiqueta, level), True


def _separar_parrafo(result: list[str]) -> None:
    if result and result[-1].strip():
        result.append("")


def _remove_waste_sections(md_text: str, stats: CleaningStats) -> str:
    """Elimina secciones completas cuyo encabezado no aporta contenido clínico."""
    result: list[str] = []
    descarte: _Descarte | None = None

    for line in md_text.split("\n"):
        texto = line.strip()
        heading = _HEADING_RE.match(line)
        if descarte and descarte.etiqueta == _ETIQUETA_DE_INDICE and heading:
            if _ENTRADA_DE_INDICE_RE.match(heading.group(2).strip()):
                continue
        if heading:
            descarte, consumida = _descarte_tras_encabezado(heading, descarte, stats)
            if not consumida:
                result.append(line)
            continue
        if descarte is None:
            result.append(line)
        elif not texto:
            _separar_parrafo(result)
        elif not _es_desecho(texto, descarte):
            result.append(line)
            if not _es_bibliografia(descarte):
                descarte = None

    return "\n".join(result)


def _cabecera_de_la_variante(
    text: str,
    known: set[str],
    min_prefix: int = 30,
) -> str | None:
    """El running header del que text es variante.

    Detecta líneas que comparten un prefijo largo (≥30
    chars) con un running header conocido — captura
    variantes truncadas o con texto extra al final.
    """
    stripped = text.strip()
    if not stripped or len(stripped) < min_prefix:
        return None
    for rh in known:
        shorter = min(len(stripped), len(rh))
        if shorter >= min_prefix:
            if stripped[:min_prefix] == rh[:min_prefix]:
                return rh
    return None


def _resto_tras_la_cabecera(texto: str, cabecera: str) -> str:
    return texto[len(os.path.commonprefix([texto, cabecera])) :].strip()


def _es_mobiliario(texto: str) -> bool:
    if not texto:
        return False
    if _MARCA_DE_MOBILIARIO_RE.search(texto):
        return True
    return (
        len(texto) <= _MAX_CARACTERES_DE_MOBILIARIO
        and texto[-1] not in _FIN_DE_FRASE
        and not _CONTENIDO_CLINICO_RE.search(texto)
    )


def _es_mobiliario_en_texto(texto: str) -> bool:
    if _MARCA_DE_MOBILIARIO_RE.search(texto):
        return True
    palabras = _PALABRA_RE.findall(texto)
    en_minuscula = sum(1 for palabra in palabras if palabra.islower())
    return _es_mobiliario(texto) and en_minuscula * 2 <= len(palabras)


def _siguiente_texto(lines: list[str], desde: int) -> str:
    for i in range(desde, len(lines)):
        texto = lines[i].strip()
        if texto and not texto.startswith("<!--"):
            return texto
    return ""


def _encabezados_que_cortan_frases(lines: list[str], candidatos: set[str]) -> set[str]:
    apariciones: dict[str, int] = {}
    cortes: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m or m.group(2).strip() not in candidatos:
            continue
        texto = m.group(2).strip()
        apariciones[texto] = apariciones.get(texto, 0) + 1
        if _CONTINUACION_DE_FRASE_RE.match(_siguiente_texto(lines, i + 1)):
            cortes[texto] = cortes.get(texto, 0) + 1
    return {texto for texto, n in apariciones.items() if cortes.get(texto, 0) * 2 > n}


def _es_encabezado_de_mobiliario(texto: str, cortan_frases: set[str]) -> bool:
    if not _es_mobiliario(texto):
        return False
    return bool(
        _MARCA_DE_MOBILIARIO_RE.search(texto)
        or _TOKEN_PEGADO_RE.search(texto)
        or texto in cortan_frases
    )


def _remove_running_headers(md_text: str, stats: CleaningStats) -> str:
    """Elimina headings y texto plano repetidos."""
    lines = md_text.split("\n")
    heading_rh, text_rh = _detect_running_headers(lines)
    cortan_frases = _encabezados_que_cortan_frases(lines, heading_rh)
    heading_rh = {
        texto for texto in heading_rh if _es_encabezado_de_mobiliario(texto, cortan_frases)
    }
    text_rh = {texto for texto in text_rh if _es_mobiliario_en_texto(texto)}
    if not heading_rh and not text_rh:
        return md_text

    result: list[str] = []
    for line in lines:
        m = _HEADING_RE.match(line)
        if m and m.group(2).strip() in heading_rh:
            stats.running_headers_removed += 1
            continue
        stripped = line.strip()
        if stripped in text_rh:
            stats.running_headers_removed += 1
            continue
        cabecera = _cabecera_de_la_variante(stripped, text_rh | heading_rh)
        if cabecera:
            stats.running_headers_removed += 1
            if resto := _resto_tras_la_cabecera(stripped, cabecera):
                result.append(resto)
            continue
        result.append(line)
    return "\n".join(result)


def _fix_broken_ligatures(text: str) -> str:
    """Repara ligaduras rotas por el extractor PDF.

    Algunos extractores separan ligaduras fi/fl con espacios:
    - "Scienti fi c" → "Scientific"
    - "Of fi ce"    → "Office"
    - "in fl uence" → "influence"
    Maneja tanto "letterfi letter" como "letter fi letter".
    """
    text = re.sub(
        r"(?<=[a-zA-ZáéíóúñüÁÉÍÓÚÑÜ])" r"fi +(?=[a-zA-Z])",
        "fi",
        text,
    )
    text = re.sub(
        r"(?<=[a-zA-ZáéíóúñüÁÉÍÓÚÑÜ])" r"fl +(?=[a-zA-Z])",
        "fl",
        text,
    )
    text = re.sub(
        r"(?<=[\s,;:(]) ?fi +(?=[a-záéíóúñü])",
        "fi",
        text,
    )
    text = re.sub(
        r"(?<=[\s,;:(]) ?fl +(?=[a-záéíóúñü])",
        "fl",
        text,
    )
    text = re.sub(
        r"(?<=[a-zA-ZáéíóúñüÁÉÍÓÚÑÜ])" r" ?fi ?- ?(?=[a-zA-Z])",
        "fi",
        text,
    )
    text = re.sub(
        r"(?<=[a-zA-ZáéíóúñüÁÉÍÓÚÑÜ])" r" ?fl ?- ?(?=[a-zA-Z])",
        "fl",
        text,
    )
    return text


def _fix_encoding_artifacts(text: str) -> str:
    """Reemplaza /C15, /C3, /C21 etc."""
    for artifact, replacement in _ENCODING_ARTIFACTS.items():
        text = text.replace(artifact, replacement)
    text = _ENCODING_RE.sub("", text)
    return text


def _remove_journal_preamble(text: str) -> str:
    """Elimina metadatos de journal pre-proof."""
    text = re.sub(r"[^\S\n]*Journal Pre-proof[^\S\n]*", " ", text)
    text = re.sub(
        r"(?<!\S)(?:\d{8},\d{4},\d{1,2},)?Downloadedfrom\S*?CommonsLicen[cs]e[^\S\n]*", "", text
    )
    text = re.sub(
        r"(?<!\S)(?:\d{8},\d{4},\d{1,2},)?Downloadedfrom\S*?(?:19|20)\d\d(?![\d/])[^\S\n]*",
        "",
        text,
    )
    text = re.sub(
        r"Downloaded\s+from\s+\S+\s+by\s+(?:[^\n.]|\.(?!\s)){0,60}?\s+on\s+"
        r"(?:\[?\d{1,2}[/ ]\w+[/ ]\d{4}\]?|\w+\s+\d{1,2},?\s+\d{4})[^\S\n]*",
        "",
        text,
    )

    preamble_patterns = [
        r"^PII:\s*$",
        r"^S\d{4}-\d{4}\(\d{2}\)\d+-\d+\s*$",
        r"^DOI:\s*$",
        r"^https?://doi\.org/.*$",
        r"^Reference:\s*$",
        r"^[A-Z]+ \d+\s*$",
        r"^To appear in:\s*$",
        r"^(Received|Revised|Accepted) Date:\s*$",
        r"^\d{1,2}\s+(January|February|March|"
        r"April|May|June|July|August|September|"
        r"October|November|December)\s+\d{4}\s*$",
        r"^Please cite this article as:.*$",
        r"^This is a PDF file of an article.*$",
        r"^Running header\s*:.*$",
        r"^Word count\s*:.*$",
        r"^Key words?:.*$",
        r"^©\s*\d{4}.*$",
        r"^\d{4}-\d{4}/©.*$",
        r"^\*\s*Correspondence to:.*$",
        r"^E-mail:\s*\S+@\S+.*$",
        r"^.*ISBN:\s*\d[\d\-]+.*$",
        r"^.*Dep[oó]sito Legal:.*$",
        r"^.*Impreso en Espa[ñn]a.*$",
        r"^.*Printed in Spain.*$",
        r"^Received:\s+\d{1,2}\s+\w+\s+\d{4}\s*$",
        r"^Accepted:\s+\d{1,2}\s+\w+\s+\d{4}\s*$",
        r"^Cite this article as:.*$",
        r"^Copyright\s+[©:].{0,80}$",
        r"^This version is distributed under" r" the terms.*$",
        r"^For commercial reproduction rights.*$",
        r"^Shareable abstract.*$",
        r"^Check for updates\s*$",
        r"^CME\s*$",
        r"^at NEJM\.org\s*$",
        r"^A Quick Take is available at.*$",
        r"^Supported by (?:a |an |the )?(?:grants?\b|NIH\b|[A-Z][\w&-]*(?: [A-Z][\w&-]*)* "
        r"(?:Foundation|Institute|Council|Fund|Award)\b)[^.\n]*\.[^\S\n]*",
        r"^Disclosure forms? provided by.*$",
        r"^A data sharing statement.*$",
        r"^Para colaborar con NCCN.*$",
        r"^NEJM\.org\s*$",
        r"^.*at NEJM\.org$",
        r"^https?://www\.ema\.europa\.eu\S*\s*$",
        r"^https?://\S+\s*$",
        r"^ORIGINAL\s+ARTICLE\s*$",
        r"^The New England Journal of Medicine\s*$",
        r"^Published\s+(?:online\s+)?\w+\s+\d{1,2},?" r"\s+\d{4}.*$",
        r"^Downloaded from\s+\S+\s*$",
        r"^Special Collection:.*$",
        r"^Cite as:.*$",
        r"^;\s*[A-Z][a-záéíóúñ]+\s+[A-Z].*$",
        r"^\d{1,2}(?:,\d{1,2})*(?:,[a-z]\))?\s*$",
        r"^\d{1,2}\s+(?:January|February|March|"
        r"April|May|June|July|August|September|"
        r"October|November|December)\s+\d{4}"
        r"\s+\d{2}:\d{2}:\d{2}\s*$",
        r"^Submitted:.*Accepted:.*Published.*$",
        r"^(?:and\s+)?[A-ZÁÉÍÓÚÑÀÈÌÒÙÜÇ]"
        r"[a-záéíóúñàèìòùüç]+"
        r"(?:[\s\-][A-ZÁÉÍÓÚÑ]"
        r"[a-záéíóúñàèìòùüç]+){1,4}"
        r",?\s+\d{1,2}(?:,\d{1,2})+"
        r"(?:,[a-z]\))?\s*$",
        r"^\*[A-Z][a-z]+(?:\s+[A-Za-z]+)*\.\*" r"\s+\d{4}.*doi:.*$",
        r"^Author Contributions\s*$",
        r"^The authors' full names.*" r"(?:Appendix|appendix).*$",
        r"^\*The complete list of.*$",
        r"^N Engl J Med \d{4};\d+:\d+[–\-]\d+" r"\.?\s*$",
    ]
    for pat in preamble_patterns:
        text = re.sub(pat, "", text, flags=re.MULTILINE)

    return text


_GUIA_NCCN_RE = re.compile(r"NCCN\s+Guidelines")


def _remove_nccn_boilerplate(text: str) -> str:
    """Elimina directorio de centros NCCN, donaciones."""
    if _GUIA_NCCN_RE.search(text):
        text = re.sub(
            r"## Dana-Farber.*",
            "",
            text,
            flags=re.DOTALL,
        )

    text = re.sub(
        r"^NCCN\.org/\S+.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^NCCNFoundation\.org\S*.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^PatientGuidelines@NCCN\.org.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^Comuniquese con nosotros.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^Complete nuestra encuesta.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^Ver NCCN Guidelines.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^La traducci[oó]n de esta NCCN.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^Para hacer una donaci[oó]n.*$",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^[¡!]Nos interesan sus comentarios.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^Please take a moment to complete.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^T[oó]mese un momento para completar.*$",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^.*NCCN\.org/patients.*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^.*nccn\.org/patientguidelines.*$",
        "",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    text = re.sub(
        r"^En la siguiente secci[oó]n de esta " r"gu[ií]a.*\.\s*$",
        "",
        text,
        flags=re.MULTILINE,
    )

    return text


_FICHA_TECNICA_RE = re.compile(
    r"ficha\s+t[eé]cnica|resumen\s+de\s+las\s+caracter[ií]sticas\s+del\s+producto|"
    r"summary\s+of\s+product\s+characteristics",
    re.IGNORECASE,
)


def _remove_ficha_tecnica_tail(text: str) -> str:
    """Elimina el final de fichas técnicas (prospecto duplicado)."""
    if not _FICHA_TECNICA_RE.search(text):
        return text
    marker = re.search(
        r"^##\s+Fecha de la [uú]ltima revisi[oó]n" r" de este prospecto",
        text,
        flags=re.MULTILINE,
    )
    if marker:
        text = text[: marker.start()].rstrip()

    marker2 = re.search(
        r"^##\s+Prospecto:\s+informaci[oó]n" r"\s+para\s+el\s+paciente",
        text,
        flags=re.MULTILINE,
    )
    if marker2:
        text = text[: marker2.start()].rstrip()

    return text


def _decode_html_entities(text: str) -> str:
    """Decodifica entidades HTML: &lt; → <, &amp; → &."""
    return _html.unescape(text)


def _remove_smpc_packaging(text: str) -> str:
    """Elimina secciones de etiquetado/embalaje de fichas.

    Los SmPC (fichas técnicas) de la EMA terminan con
    ANEXO III → etiquetado de envases, blisters, braille,
    y a veces el prospecto duplicado. Nada de esto tiene
    valor clínico.
    """
    if not _FICHA_TECNICA_RE.search(text):
        return text
    markers = [
        r"^#+\s+ANEXO\s+III",
        r"^#+\s+A\.\s+ETIQUETADO",
        r"^#+\s+B\.\s+PROSPECTO",
    ]
    for pattern in markers:
        m = re.search(pattern, text, flags=re.MULTILINE)
        if m:
            text = text[: m.start()].rstrip()
            break
    return text


def _remove_inline_toc(text: str) -> str:
    """Elimina tablas de contenido inline.

    Detecta "ÍNDICE" como texto plano seguido de una
    tabla Markdown, y líneas con dot leaders (....)
    seguidas de números de página.
    """
    text = re.sub(
        r"^[ÍI]NDICE\s*$\n+" r"(^\|.*$\n?)+",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^.*\.{6,}.*\d+\s*$",
        "",
        text,
        flags=re.MULTILINE,
    )
    return text


def _remove_author_affiliations(md_text: str) -> str:
    """Elimina bloques de afiliaciones de autores.

    Detecta patrones como:
    - Superscript-numbered affiliations (¹ Hospital X...)
    - Numbered affiliations (1 Department of...)
    - "Corresponding author:" lines
    - "ORIGINAL ARTICLE" journal labels
    """
    md_text = re.sub(
        r"(^[¹²³⁴⁵⁶⁷⁸⁹⁰]+\s+.{10,120}$\n?){3,}",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"(^\d{1,2}\s+(?:Department|Division|"
        r"Faculty|School|Institute|Hospital|"
        r"Centro|Servicio|Departamento|"
        r"Unidad|University|Université|"
        r"Universität).{10,200}$\n?){3,}",
        "",
        md_text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    md_text = re.sub(
        r"^Corresponding author:.*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^ORIGINAL\s+ARTICLE\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^(?:Address for correspondence|" r"Reprint requests?):.*$",
        "",
        md_text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    md_text = re.sub(
        r"^\*\s*Correspondence to\s*:.*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^RESEARCH\s+ARTICLE\s*[\|:]?.*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^#{0,3}\s*(?:Case Report|SPECIAL ARTICLE|"
        r"SHORT COMMUNICATION|REVIEW ARTICLE|"
        r"BRIEF REPORT|CLINICAL STUDY|LETTER)\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )
    md_text = re.sub(
        r"^OPEN\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^#{0,3}\s*HHS Public Access\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )
    md_text = re.sub(
        r"^Author [Mm]anuscript\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )
    md_text = re.sub(
        r"^Published in final edited form as:\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^\*\*[A-ZÁ-Ú][a-záéíóúñ]+"
        r"(?:\s+[A-ZÁ-Ú]\.?[a-záéíóúñ\-]*)*"
        r",?\*\*\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^[A-ZÁ-Ú][a-záéíóúñ\-]+"
        r"(?:\s+[A-ZÁ-Ú]\.?)?"
        r"(?:\s+[A-ZÁ-Ú][a-záéíóúñ\-]+)*"
        r"<sup>[^<]+</sup>"
        r"(?:\s+and\s+.*<sup>[^<]+</sup>)?\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^Corresponding Author:.*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^(?:Division|Department|Centre|Center|"
        r"Research Center|School|Faculty|Institute"
        r") of .{10,200}$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^Received\s+\d{1,2}\s+\w+\s+\d{4}\s+"
        r"Revised\s+\d{1,2}\s+\w+\s+\d{4}\s+"
        r"Accepted\s+\d{1,2}\s+\w+\s+\d{4}\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^\*?[A-Z][a-z]+(?:\s+[A-Za-z]+){0,5}"
        r"[\.\*]*\s*\d{4}\s*[;,]\s*"
        r"\d+(?:\(\d+\))?\s*:\s*\d+[–\-]\d+"
        r"\.?\s*(?:doi:.*)?$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^J\s+Hypertens\s+\d+:\d+[–\-]\d+\s+" r"Copyright\s+©.*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )
    md_text = re.sub(
        r"^(?:This is an open access article|"
        r"This article is distributed)"
        r"\s+(?:distributed\s+)?under\s+the\s+"
        r"terms\s+of\s+the\s+Creative\s+Commons"
        r".*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^DOI:\s*10\.\d{4,}/\S+\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^Correspondence to\s+.*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^(?:Journal of \w+\s+)?www\.\w+\.com\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^Volume\s+\d+\s*\.\s*Number\s+\d+" r"\s*\.\s*\w+\s+\d{4}\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    return md_text


def _remove_figure_content(md_text: str) -> str:
    """Elimina las referencias a figuras que no llevan contenido.

    - Etiquetas de panel huerfanas (a), b), c), d))
    - Referencias inline a figuras en parentesis
    """
    md_text = re.sub(
        r"^\s*[a-d]\)\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^No\.\s+at\s+Risk\s*$",
        "",
        md_text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    md_text = re.sub(
        r"\s*\((?:(?:see|ver)\s+)?(?:Fig(?:ure|ura)?s?\.?\s+" r"S?\d+[^)]*)\)",
        "",
        md_text,
        flags=re.IGNORECASE,
    )

    md_text = re.sub(
        r"\s*\[(?:Fig(?:ure)?s?\.?\s+" r"S?\d+[^\]]*)\]",
        "",
        md_text,
        flags=re.IGNORECASE,
    )

    md_text = re.sub(
        r"^#+\s+Highlights\s+\(online only\):" r"?\s*$.*?(?=\n##|\Z)",
        "",
        md_text,
        flags=re.MULTILINE | re.DOTALL,
    )

    return md_text


def _rejoin_broken_sentences(md_text: str) -> str:
    """Reune oraciones rotas por saltos de pagina.

    Los extractores PDF a veces rompen oraciones entre paginas,
    dejando un parrafo que termina sin puntuacion final
    seguido de una linea en blanco y otro parrafo que
    empieza en minuscula. Esta funcion los reune.

    Una línea que **empieza por `|`** no se une, y de eso ya se encarga la comprobación de
    minúscula: `|` no lo es. Había además una guarda explícita `not startswith("|")`, inalcanzable
    por esa razón —ninguna mutación suya podía morir— y por eso se quitó. Ojo con enunciarlo como
    «una fila de tabla no se une», que sería más de lo que hace: una fila GFM sin pipe inicial sí
    se une. No es un problema en la práctica porque opendataloader lo emite siempre.
    """
    lines = md_text.split("\n")
    result: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip()
        if (
            stripped
            and not stripped.startswith("#")
            and not stripped.startswith("|")
            and not stripped.startswith("- ")
            and not stripped.startswith("* ")
            and not stripped.startswith("> ")
            and not stripped.startswith("```")
            and len(stripped) > 20
            and stripped[-1] not in _FIN_DE_FRASE
            and stripped[-1] != "|"
        ):
            j = i + 1
            blank_count = 0
            while j < n and not lines[j].strip():
                blank_count += 1
                j += 1
            if blank_count == 1 and j < n and lines[j].strip() and lines[j].strip()[0].islower():
                result.append(stripped + " " + lines[j].strip())
                i = j + 1
                continue
        result.append(line)
        i += 1
    return "\n".join(result)


def _remove_docling_artifacts(md_text: str) -> str:
    """Elimina artefactos estructurales del extractor PDF.

    - ``## Page N`` headings (marcadores de pagina)
    - ``---`` separadores de pagina
    - ``<mermaid>...</mermaid>`` diagramas de flujo
    - ``REFERENCES`` como texto plano (sin heading)
    """
    md_text = re.sub(
        r"^#{1,3}\s+Page\s+\d+\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^---\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"<mermaid>.*?</mermaid>",
        "",
        md_text,
        flags=re.DOTALL,
    )

    md_text = re.sub(
        r"^REFERENCES\s*$",
        "## REFERENCES",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^(#{1,6})\s+\1\s+",
        r"\1 ",
        md_text,
        flags=re.MULTILINE,
    )

    lines = md_text.split("\n")
    seen_headings: set[str] = set()
    dedup_limit = min(100, len(lines))
    deduped: list[str] = []
    for i, line in enumerate(lines):
        if i < dedup_limit:
            m = _HEADING_RE.match(line)
            if m:
                key = m.group(2).strip().lower()
                if key in seen_headings:
                    continue
                seen_headings.add(key)
        deduped.append(line)
    md_text = "\n".join(deduped)

    return md_text


def _remove_journal_running_headers(
    md_text: str,
) -> str:
    """Elimina running headers de revistas conocidas.

    Patrones que se repiten por pagina y varian
    ligeramente (numero de pagina, issue), por lo que
    el detector generico de repeticiones puede fallar.
    """
    patterns = [
        r"^EUROPEAN RESPIRATORY JOURNAL\s*$",
        r"^ORIGINAL RESEARCH ARTICLE.*$",
        r"^Eur Respir J \d{4};\s*\d+:\s*\d+\s*$",
        r"^N ENGL J MED \d+;\d+.*$",
        r"^T h e\s+n e w\s+e n g l a n d.*$",
        r"^ESC/ERS Guidelines\s*$",
        r"^ESC GUIDELINES\s*$",
        r"^[A-Z]\.\s+[A-Z]+\s+ET AL\.?\s*$",
        r"^[A-Z][a-z]+(?:\s+(?:et\s+al|and\s+" r"[A-Z][a-z]+))\.?\s*$",
        r"^Eur Heart J.*\d{4}.*\d+[,–-]\d+\s*$",
        r"^Annals of Oncology.*\d{4}.*$",
        r"^APL Mach\. Learn\..*$",
        r"^J\.\s+Chem\.\s+Phys\..*\d{4}.*$",
        r"^AIP Conf\.\s+Proc\..*$",
    ]
    for pat in patterns:
        md_text = re.sub(pat, "", md_text, flags=re.MULTILINE)
    return md_text


def _inline_cleanup(md_text: str) -> str:
    """Limpieza inline final."""
    md_text = re.sub(r"<!-- image -->", "", md_text)
    md_text = re.sub(r"\[IMAGEN\]", "", md_text)

    for tag in (
        "page_number",
        "footer",
        "header",
        "watermark",
    ):
        md_text = re.sub(
            rf"<{tag}>.*?</{tag}>",
            "",
            md_text,
            flags=re.DOTALL,
        )
    md_text = re.sub(
        r"<img>.*?</img>",
        "",
        md_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    md_text = re.sub(
        r"<table>.*?</table>",
        "",
        md_text,
        flags=re.DOTALL,
    )

    md_text = re.sub(
        r"Downloaded from[^\n]*?All rights reserved\.",
        "",
        md_text,
    )
    md_text = re.sub(
        r"Copyright[^\n]*?All rights reserved\.",
        "",
        md_text,
    )

    if "AFFILIATIONS" in md_text:
        md_text = re.sub(
            r"^#{1,6}\s+AFFILIATIONS.*?(?=^##|\Z)",
            "",
            md_text,
            flags=re.DOTALL | re.MULTILINE,
        )

    md_text = re.sub(
        r"^(?:Conflict of Interest|" r"Conflicts? of Interests?):?\s.*$",
        "",
        md_text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    md_text = re.sub(r"\S+@\S+\.\S+", "", md_text)
    md_text = re.sub(
        r"\+\d{1,3}\s*\(?\d{1,4}\)?[\s\.\-]"
        r"\d{2,4}[\s\.\-]\d{2,4}[\s\.\-]?\d{0,4}",
        "",
        md_text,
    )

    md_text = re.sub(
        r"^\d{1,4}\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^[A-Za-z\u00C0-\u024F]\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^[†‡§¶*]{1,3}\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^(Purple|Blue|Turquoise|White|Orange|"
        r"Green|Red|Yellow|Dark green|Dashed):"
        r"\s+.{0,200}$",
        "",
        md_text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    md_text = re.sub(
        r"^\[SECCI[OÓ]N ELIMINADA:.*?\]\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(
        r"^-\s*$",
        "",
        md_text,
        flags=re.MULTILINE,
    )

    md_text = re.sub(r"\n{3,}", "\n\n", md_text)
    md_text = re.sub(r" {2,}", " ", md_text)

    return md_text.strip()


_MAX_RUN = 200

_PATHOLOGICAL_RUN = re.compile(rf"(.)\1{{{_MAX_RUN - 1},}}", re.DOTALL)
_LONG_WHITESPACE = re.compile(rf"\s{{{_MAX_RUN},}}")


def _collapse_whitespace_blob(match: re.Match[str]) -> str:
    """Una tirada larga de blanco se reduce a un salto de párrafo, o a un espacio."""
    return "\n\n" if "\n" in match.group(0) else " "


def _collapse_pathological_runs(text: str) -> str:
    """Acota cualquier carácter repetido cientos de veces, antes de que lo vean las regex.

    Es una **guarda de disponibilidad**, no cosmética, y cubre una clase entera de fallo. Este
    módulo aplica más de 200 regex sobre texto que controla al completo quien sube el PDF, y
    varias tienen la forma `.*` … cuantificador … `.*`, que sobre una tirada larga del mismo
    carácter obliga al motor a probar todas las formas de repartirla. Medido antes de esta
    guarda: 2.000 espacios, 2.000 saltos o 2.000 puntos tardaban más de 25 segundos cada uno.
    Como toda la ingesta pasa por una sola cola Celery, un solo documento así la bloquea.

    Hay dos reglas porque la primera versión, que solo acotaba caracteres **idénticos**
    consecutivos, la burlaba el blanco alternado (`' \\n'` repetido): `\\s` lo trata como una
    sola clase, así que hace falta acotar también tiradas de blanco mezclado.

    Arreglar patrón a patrón sería jugar al topo; acotar la entrada corta los tres casos
    encontrados y los que queden por encontrar. Ninguna tirada de más de `_MAX_RUN` caracteres
    idénticos lleva información clínica, así que en documentos reales no cambia nada. Sí es una
    diferencia deliberada frente al paquete para entradas patológicas, y la fija el fuzz.
    """
    text = _LONG_WHITESPACE.sub(_collapse_whitespace_blob, text)
    return _PATHOLOGICAL_RUN.sub(lambda m: m.group(1) * _MAX_RUN, text)


@overload
def clean_markdown(md_text: str, return_stats: Literal[False] = False) -> str: ...


@overload
def clean_markdown(md_text: str, return_stats: Literal[True]) -> tuple[str, CleaningStats]: ...


def clean_markdown(
    md_text: str,
    return_stats: bool = False,
) -> str | tuple[str, CleaningStats]:
    """Limpia Markdown eliminando contenido sin valor clínico.

    Args:
        md_text: Markdown crudo del extractor PDF.
        return_stats: Si True, devuelve (texto, stats).

    Returns:
        Markdown limpio, o (markdown, CleaningStats).
    """
    stats = CleaningStats(original_chars=len(md_text))

    md_text = _collapse_pathological_runs(md_text)

    md_text = _decode_html_entities(md_text)

    md_text = fix_ligatures(md_text)

    md_text = _fix_broken_ligatures(md_text)

    md_text = _fix_encoding_artifacts(md_text)

    md_text = _remove_docling_artifacts(md_text)
    md_text = _collapse_pathological_runs(md_text)

    md_text = _remove_journal_preamble(md_text)

    md_text = _remove_journal_running_headers(md_text)

    md_text = _remove_author_affiliations(md_text)

    md_text = _remove_waste_sections(md_text, stats)

    md_text = _remove_stray_references(md_text)
    md_text = _remove_reference_runs(md_text)
    md_text = _unir_formulas_partidas(md_text)
    md_text = _arreglar_glifos(md_text)

    md_text = _rejoin_broken_sentences(md_text)

    md_text = _remove_figure_content(md_text)

    md_text = _remove_running_headers(md_text, stats)

    md_text = _remove_nccn_boilerplate(md_text)

    md_text = _remove_smpc_packaging(md_text)

    md_text = _remove_ficha_tecnica_tail(md_text)

    md_text = _remove_inline_toc(md_text)

    md_text = _inline_cleanup(md_text)

    stats.cleaned_chars = len(md_text)

    if return_stats:
        return md_text, stats
    return md_text


def clean_markdown_file(input_path: str | Path, output_path: str | Path) -> None:
    """Lee un Markdown, lo limpia y guarda el resultado."""
    input_path = Path(input_path)
    output_path = Path(output_path)

    from .pdf_converter import sanitize_text_to_utf8

    md_content = input_path.read_bytes().decode("utf-8", errors="replace")
    md_content = sanitize_text_to_utf8(md_content)
    cleaned, stats = clean_markdown(md_content, return_stats=True)
    output_path.write_text(cleaned, encoding="utf-8")

    import logging as _logging

    _logger = _logging.getLogger(__name__)

    _logger.info("Limpiado: %s", input_path.name)
    _logger.info(
        "  Original: %s chars → Limpio: %s chars (%.1f%% reducción)",
        f"{stats.original_chars:,}",
        f"{stats.cleaned_chars:,}",
        stats.reduction_pct,
    )
    if stats.sections_removed:
        joined = ", ".join(stats.sections_removed)
        _logger.info("  Secciones eliminadas: %s", joined)
    if stats.running_headers_removed:
        _logger.info(
            "  Running headers eliminados: %d",
            stats.running_headers_removed,
        )