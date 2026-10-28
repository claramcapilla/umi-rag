"""Sanitización completa de JSON generado por LLM.

Preprocesa el texto crudo del LLM (code fences, truncamiento,
caracteres inválidos) y post-procesa el dict parseado
(normalización de valores, tipos, campos faltantes).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from src.document_processor.schema import ClinicalJson, DocVariant, SectionMap
from src.document_processor.sections import SECTIONS_BY_VARIANT

from .contenido import (
    CADENA_NULA,
    CLAVE_FILAS,
    CLAVES_POSICIONALES,
    MARCADORES_DE_AUSENCIA,
    quitar_duplicados,
    recortar_palabras_clave,
)

logger = logging.getLogger(__name__)

_VALOR_DEL_ESQUEMA_RE = re.compile(r':\s*\[?\s*"([^"\n]{15,160})"')


def _clave_de_plantilla(texto: str) -> str:
    return " ".join(texto.strip().strip("[]").split()).casefold()


_VALORES_LEGITIMOS = frozenset({"clinical_guideline", "no significativo"})
_VALORES_DEL_ESQUEMA = (
    frozenset(
        _clave_de_plantilla(m.group(1))
        for secciones in SECTIONS_BY_VARIANT.values()
        for seccion in secciones.values()
        for m in _VALOR_DEL_ESQUEMA_RE.finditer(seccion.get("description", ""))
    )
    - _VALORES_LEGITIMOS
)

_SOBRE_EL_DOCUMENTO_RE = re.compile(
    r"^(?:(?:not|none)(?:\s+(?:explicitly|specifically|clearly))?"
    r"\s+(?:mentioned|specified|detailed|defined|discussed|stated)"
    r"|no(?:\s+se)?(?:\s+(?:expl[ií]citamente|espec[ií]ficamente))?"
    r"\s+(?:menciona|especifica|detalla|define|discute|mencionad[oa]|especificad[oa]"
    r"|detallad[oa]|definid[oa]|discutid[oa])s?n?)\b"
    r"[^.;\n]{0,60}?\b(?:in|en|del?)\s+"
    r"(?:the|this|el|este|esta|la)\s+(?:provided\s+|proporcionado\s+)?"
    r"(?:document|documento|extracto|text|texto|article|art[ií]culo|excerpt)\b"
    r"(?:\s+(?:provided|proporcionad[oa]))?[\s.]*$",
    re.IGNORECASE,
)


def _strip_code_fences(text: str) -> str:
    """Elimina code fences markdown (```json ... ```)."""
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?", text, re.IGNORECASE)
    if m:
        text = text[m.end() :]  # noqa: E203
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _strip_surrounding_text(text: str) -> str:
    """Extrae el objeto JSON si hay texto envolvente."""
    text = text.strip()
    if text.startswith("{"):
        return text
    idx = text.find("{")
    if idx > 0:
        text = text[idx:]
    return text


def _fix_trailing_commas(text: str) -> str:
    """Elimina trailing commas antes de } o ]."""
    return re.sub(r",\s*([}\]])", r"\1", text)


def _strip_json_comments(text: str) -> str:
    """Elimina comentarios `//` que estén fuera de un string.

    Heurística deliberadamente conservadora: solo comentarios de línea, y rastreando comillas
    y escapes para no cortar una URL (`https://…`) que viva dentro de un valor.
    """
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        in_string = False
        escape_next = False
        comment_pos = None
        for i, ch in enumerate(line):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
            elif ch == "/" and not in_string and i + 1 < len(line) and line[i + 1] == "/":
                comment_pos = i
                break
        if comment_pos is not None:
            line = line[:comment_pos].rstrip()
        cleaned.append(line)
    return "\n".join(cleaned)


_CLOSERS = {"[": "]", "{": "}"}


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _unescaped_chars(text: str) -> Iterator[str]:
    """Recorre el texto saltándose los caracteres escapados.

    Lo comparten el conteo de comillas y el de delimitadores: sin esto, un `\\"` dentro de
    un valor contaría como fin de string y la reparación cerraría la estructura mal.
    """
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        yield ch


def _has_unclosed_string(line: str) -> bool:
    return sum(1 for ch in _unescaped_chars(line) if ch == '"') % 2 != 0


def _drop_last_line_if_string_unclosed(text: str) -> str:
    """Descarta la última línea si se cortó a mitad de un string.

    Solo si queda alguna línea más: con una sola línea, tirarla dejaría el texto vacío y se
    perdería todo lo extraíble.
    """
    lines = text.split("\n")
    if len(lines) > 1 and _has_unclosed_string(lines[-1]):
        return "\n".join(lines[:-1])
    return text


def _unclosed_delimiters(text: str) -> list[str]:
    """Delimitadores abiertos y sin cerrar, en orden de apertura.

    Ignora lo que hay dentro de strings: una llave en un valor de texto no abre estructura.
    """
    stack: list[str] = []
    in_string = False
    for ch in _unescaped_chars(text):
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in _CLOSERS:
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    return stack


def _repair_truncated_json(text: str) -> str:
    """Intenta reparar JSON truncado cerrando llaves/corchetes.

    Cuando max_tokens corta la respuesta a mitad de un objeto,
    el JSON queda con llaves/corchetes sin cerrar. Este método
    intenta cerrar la estructura de forma conservadora.
    """
    text = text.rstrip()
    if not text:
        return text

    if _is_valid_json(text):
        return text

    text = _drop_last_line_if_string_unclosed(text)
    text = text.rstrip().rstrip(",")

    pending = _unclosed_delimiters(text)
    if pending:
        logger.warning(
            "JSON truncado detectado: %d delimitadores sin cerrar. Intentando reparar...",
            len(pending),
        )
        text += "".join(_CLOSERS[opener] for opener in reversed(pending))

    return text


def preprocess_llm_output(raw: str) -> str:
    """Pipeline completo de preprocesado del texto crudo LLM.

    Aplica en orden:
    1. Eliminar code fences
    2. Eliminar texto envolvente
    3. Eliminar comentarios
    4. Eliminar trailing commas
    5. Reparar truncamiento
    """
    text = _strip_code_fences(raw)
    text = _strip_surrounding_text(text)
    text = _strip_json_comments(text)
    text = _fix_trailing_commas(text)
    text = _repair_truncated_json(text)
    return text


def _texto_limpio(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"[\ufffd\u200b\u200c\u200d\ufeff\u00ad]", "", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _texto_ausente(value: str) -> bool:
    return not value or value in MARCADORES_DE_AUSENCIA or bool(_SOBRE_EL_DOCUMENTO_RE.match(value))


def _clean_string_value(value: str) -> str | None:
    value = _texto_limpio(value)
    if _texto_ausente(value) or _clave_de_plantilla(value) in _VALORES_DEL_ESQUEMA:
        return None
    return value


def _posicion_saneada(elemento: Any) -> Any:
    if not isinstance(elemento, str):
        return _sanitize_value(elemento)
    texto = _texto_limpio(elemento)
    return None if _texto_ausente(texto) else texto


def _celda_saneada(celda: Any) -> Any:
    if not isinstance(celda, str):
        return _sanitize_value(celda)
    texto = _texto_limpio(celda)
    return None if not texto or texto == CADENA_NULA else texto


def _sanitize_value(value: Any) -> Any:
    """Sanitiza recursivamente un valor del JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return _clean_string_value(value)
    if isinstance(value, list):
        cleaned = [_sanitize_value(item) for item in value]
        return [item for item in cleaned if item is not None]
    if isinstance(value, dict):
        return {k: _valor_saneado(k, v) for k, v in value.items()}
    return value


def _valor_saneado(clave: str, valor: Any) -> Any:
    if not isinstance(valor, list):
        return _sanitize_value(valor)
    if clave == CLAVE_FILAS:
        return _filas_saneadas(valor)
    if clave in CLAVES_POSICIONALES:
        return [_posicion_saneada(elemento) for elemento in valor]
    return _sanitize_value(valor)


def _filas_saneadas(filas: list[Any]) -> list[Any]:
    return [
        [_celda_saneada(celda) for celda in fila]
        if isinstance(fila, list)
        else _sanitize_value(fila)
        for fila in filas
    ]


_INT_FIELDS = {"year", "n_patients", "n_studies_included", "source_pages"}
_YEAR_FIELDS = {"year"}
_NESTED_INT_FIELDS = ("n_patients",)
_ANY_DIGITS = re.compile(r"(\d{1,3}(?:[.,\u00a0\u202f]\d{3})+(?!\d|[.,]\d)|\d+)")
_SEPARADOR_DE_MILLARES = re.compile(r"[.,\u00a0\u202f]")
_FOUR_DIGITS = re.compile(r"(\d{4})")


def _int_from_text(text: str, key: str) -> int | None:
    """Primer número del texto, o `None` si no hay ninguno.

    El LLM devuelve cosas como "240 pacientes" o "2024 (revisado)". A los años se les exige
    cuatro dígitos, para no leer el "12" de "12 meses de seguimiento" como si fuese el año.
    """
    patron = _FOUR_DIGITS if key in _YEAR_FIELDS else _ANY_DIGITS
    encontrado = patron.search(text)
    return int(_SEPARADOR_DE_MILLARES.sub("", encontrado.group(1))) if encontrado else None


def _int_from_text_logged(text: str, key: str) -> int | None:
    """Como `_int_from_text`, dejando rastro del campo pero **nunca de su valor**.

    El valor es contenido del documento y estos registros van a un destino cuya retención
    no controlamos: basta el nombre del campo para diagnosticar.
    """
    numero = _int_from_text(text, key)
    if numero is None:
        logger.warning("Campo '%s' no convertible a int; se deja en null", key)
    else:
        logger.debug("Campo '%s' convertido a entero", key)
    return numero


def _coerced_int(value: Any, key: str) -> Any:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else value
    if isinstance(value, str):
        return _int_from_text_logged(value, key)
    return value


def _coerce_nested_population_ints(data: ClinicalJson) -> None:
    poblacion = data.get("population")
    if not isinstance(poblacion, dict):
        return
    for key in _NESTED_INT_FIELDS:
        if isinstance(poblacion.get(key), str):
            poblacion[key] = _int_from_text(poblacion[key], key)


def _coerce_integer_fields(data: ClinicalJson) -> ClinicalJson:
    """Convierte a entero (o a null) los campos que el esquema declara numéricos."""
    for key in _INT_FIELDS:
        if key in data:
            data[key] = _coerced_int(data[key], key)
    _coerce_nested_population_ints(data)
    return data


def _ensure_list_fields(data: ClinicalJson, doc_type: DocVariant) -> ClinicalJson:
    """Asegura que campos que deben ser listas sean listas.

    Si un campo de lista contiene un string, lo envuelve en [].
    Si es null, lo convierte en [].
    """
    list_fields = {
        "authors",
        "keywords",
        "objectives",
        "interventions",
        "comparators",
        "subgroup_analyses",
        "secondary_results",
        "clinical_implications",
        "practice_implications",
        "limitations",
        "strengths",
        "open_questions",
        "future_directions",
        "main_findings",
        "diseases_and_conditions",
        "drugs_and_therapies",
        "biomarkers_and_genes",
        "scales_and_classifications",
        "key_numeric_data",
        "tables",
        "indications",
        "contraindications",
        "warnings_and_precautions",
        "excipients",
        "marketing_authorisation_number",
        "conditions",
        "treatment_pathways",
        "therapies_index",
        "recommendations",
        "artifacts",
        "special_populations",
        "glossary",
        "abbreviations",
        "toc",
    }

    if doc_type == "guia_clinica":
        list_fields.discard("keywords")
    for key in list_fields:
        if key not in data:
            continue
        val = data[key]
        if val is None:
            data[key] = []
        elif isinstance(val, str):
            data[key] = [val] if val.strip() else []
        elif not isinstance(val, list):
            data[key] = [val]

    return data


def _ensure_required_fields(
    data: ClinicalJson,
    schema: SectionMap,
) -> list[str]:
    """Inicializa a null los campos del esquema que falten, y devuelve cuáles eran.

    Devolver la lista es la costura para el PR del orquestador. Hoy rellenar un campo
    ausente con `null` **borra la evidencia** de que su sección falló en el LLM, y quien
    decide seguir adelante y dar el documento por listo es `clinical_formatter`. Con esta
    lista podrá distinguir "el documento no traía ese dato" de "esa sección no se generó",
    que es la diferencia entre un JSON incompleto y una guía indexada sin su posología.
    """
    ausentes = [
        field
        for section_info in schema.values()
        for field in section_info["fields"]
        if field not in data
    ]
    for field in ausentes:
        data[field] = None
    if ausentes:
        logger.debug("Campos ausentes inicializados a null: %d", len(ausentes))
    return ausentes


def sanitize_clinical_json(
    data: ClinicalJson,
    doc_type: DocVariant,
) -> ClinicalJson:
    """Sanitización completa del JSON clínico ya parseado.

    `doc_type` es una `DocVariant`: el sistema tiene un solo vocabulario.
    """
    sanitizado: ClinicalJson = _sanitize_value(data)
    sanitizado = _coerce_integer_fields(sanitizado)
    sanitizado = _ensure_list_fields(sanitizado, doc_type)
    quitar_duplicados(sanitizado)
    recortar_palabras_clave(sanitizado)

    _ensure_required_fields(sanitizado, SECTIONS_BY_VARIANT[doc_type])

    return sanitizado


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _as_clinical_json(parsed: Any, section_key: str) -> ClinicalJson:
    """Exige que lo parseado sea un objeto JSON.

    `json.loads` acepta escalares: una respuesta como `42` o `"texto"` parsea sin error y
    devuelve un `int` o un `str`. Sin esta comprobación el valor viajaba como si fuera un
    dict y reventaba mucho más adelante, con un `TypeError` dentro del relleno de campos, en
    vez de fallar en el borde con el nombre de la sección.
    """
    if not isinstance(parsed, dict):
        raise ValueError(
            f"La sección '{section_key}' no devolvió un objeto JSON, sino {type(parsed).__name__}"
        )
    return parsed


def safe_parse_llm_json(
    raw_content: str,
    section_key: str,
) -> ClinicalJson:
    """Parsea y sanea el output crudo del LLM, con fallback ante truncamiento.

    Lanza `ValueError` si el JSON es irrecuperable o si no es un objeto.

    **El mensaje no lleva contenido del documento**: identifica el caso por longitud y huella.
    Esto ya está en el camino de ejecución: el flip de `src/pipeline/tasks.py` a la copia in-repo
    se hizo en la PR 7a de la Fase 7, así que los 500 caracteres de respuesta del LLM que el
    paquete metía en el mensaje —y que salían por el `logger.error` de `clinical_formatter`— ya
    no se emiten.
    """
    preprocessed = preprocess_llm_output(raw_content)

    try:
        directo = json.loads(preprocessed)
    except json.JSONDecodeError:
        pass
    else:
        return _as_clinical_json(directo, section_key)

    lines = preprocessed.strip().split("\n")
    for drop in range(1, min(4, len(lines))):
        attempt = "\n".join(lines[:-drop])
        attempt = attempt.rstrip().rstrip(",")
        attempt = _repair_truncated_json(attempt)
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        logger.warning(
            "JSON de sección '%s' recuperado tras eliminar %d líneas truncadas del final",
            section_key,
            drop,
        )
        return _as_clinical_json(parsed, section_key)

    raise ValueError(
        f"JSON irrecuperable en sección '{section_key}' "
        f"({len(raw_content)} caracteres, sha256 {_fingerprint(raw_content)})"
    )