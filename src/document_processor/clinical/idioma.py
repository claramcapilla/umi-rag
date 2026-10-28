"""Detección y normalización de idiomas para el pipeline clínico."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

ISO_POR_IDIOMA = {
    "alemán": "de",
    "español": "es",
    "francés": "fr",
    "inglés": "en",
    "italiano": "it",
    "neerlandés": "nl",
    "portugués": "pt",
}

_WORDS_BY_LANGUAGE = {
    "alemán": {"aber", "auch", "auf", "bei", "das", "der", "die", "ein", "eine", "für", "mit", "und", "von"},
    "español": {"al", "como", "con", "del", "el", "en", "es", "la", "las", "los", "para", "por", "que", "se", "una", "y"},
    "francés": {"avec", "dans", "de", "des", "du", "elle", "et", "la", "les", "pour", "que", "sur", "une"},
    "inglés": {"a", "an", "and", "are", "as", "for", "from", "in", "is", "of", "on", "that", "the", "this", "to", "with"},
    "italiano": {"che", "con", "da", "del", "della", "di", "e", "gli", "il", "la", "per", "sono", "un", "una"},
    "neerlandés": {"aan", "als", "bij", "dat", "de", "een", "en", "het", "in", "met", "van", "voor"},
    "portugués": {"com", "da", "das", "de", "do", "dos", "e", "em", "na", "não", "o", "para", "que", "uma"},
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_LANGUAGE_ALIASES = {
    "de": "de", "deutsch": "de", "german": "de", "alemán": "de", "aleman": "de",
    "es": "es", "español": "es", "espanol": "es", "spanish": "es",
    "fr": "fr", "français": "fr", "francais": "fr", "french": "fr", "francés": "fr", "frances": "fr",
    "en": "en", "english": "en", "inglés": "en", "ingles": "en",
    "it": "it", "italiano": "it", "italian": "it",
    "nl": "nl", "dutch": "nl", "neerlandés": "nl", "neerlandes": "nl",
    "pt": "pt", "português": "pt", "portugues": "pt", "portuguese": "pt",
}


def detectar_idioma(texto: str) -> str:
    """Estima el idioma predominante y devuelve un nombre normalizado en español."""
    words = Counter(word.casefold() for word in _WORD_RE.findall(texto))
    if not words:
        return "español"

    scores = {
        language: sum(words[word] for word in markers)
        for language, markers in _WORDS_BY_LANGUAGE.items()
    }
    if any(char in texto for char in "áéíóúñ¿¡"):
        scores["español"] += 2
    if any(char in texto for char in "àâçèêëîïôùûüÿœ"):
        scores["francés"] += 2
    if any(char in texto for char in "ãõ"):
        scores["portugués"] += 2

    language = max(scores, key=scores.__getitem__)
    return language if scores[language] else "español"


def normalizar_idioma(valor: object) -> str:
    """Normaliza un nombre de idioma o un código ISO 639 a minúsculas."""
    if not isinstance(valor, str):
        return ""
    language = unicodedata.normalize("NFC", valor).strip().casefold().replace("_", "-")
    language = re.sub(r"\s+", " ", language)
    if language in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[language]
    return language if re.fullmatch(r"[a-z]{2,3}", language) else ""


__all__ = ["ISO_POR_IDIOMA", "detectar_idioma", "normalizar_idioma"]
