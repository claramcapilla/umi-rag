"""Capas 0, 1, 2 y el núcleo de la 3 del revisor de fidelidad: deterministas, sin LLM ni veredicto.

La capa 0 mide la calidad de conversión (capitulares emitidas como encabezado, secciones sin
clasificar), que explica los fallos de las capas de abajo en vez de solo detectarlas. La capa 1
mide la cobertura de entrada con las dos métricas que el plan exige distinguir: la unión global
—texto que ninguna sección vio— y la cobertura por sección, que es la que encontró las pérdidas
reales (un extracto de 12.032 sobre 87.358 caracteres con la métrica global al 100%). La capa 2
ancla la salida contra el fuente: números y fechas por su secuencia, los nombres verbatim por
contrato (fármaco, biomarcador, escala) por subcadena, y el resto por términos raros —cada campo
de procedencia-documento—, declarando sin veredicto lo no anclado para que la fase 4 mida el corte.

`salient_recall` es el núcleo determinista de la capa 3: pregunta por presencia concreta —¿los
términos raros de cada sección del fuente llegaron a algún campo de procedencia-documento del
JSON?—, que es inmune a la varianza de 5x entre ejecuciones que invalida comparar recuentos. Cubre
la omisión, la familia de fallo que hemos pagado cuatro veces. El resto de la capa 3 —fármacos con
vocabulario de CIMA, tablas del Markdown ausentes de `artifacts`, ratio de frases deónticas— cablea
nombres de campo del esquema, justo lo que el plan prohíbe («las listas se derivan de
`SECTIONS_BY_VARIANT`, nunca se cablean»), así que espera a la fase 5, donde el mapeo de campos y
los umbrales se construyen a la vez.

Aquí no hay umbrales: se miden en la fase 4 del plan (docs/FIDELITY_REVIEW_PLAN.md) y decidir
es del llamante, el mismo reparto que `_extraction`. Los ratios salen redondeados a 4 decimales
para que el informe sea estable al serializarlo, no por precisión: ninguna capa dictamina.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

from document_processor.schema import DocVariant
from document_processor.sectioning import (
    DocumentSection,
    SectionCategory,
    SectionChunk,
    document_has_structure,
)
from document_processor.sections import field_provenance, verbatim_fields

REPORT_VERSION = 4
"""4 añade el bloque `salient` (núcleo de la capa 3) al informe del camino del PDF.

3 añadió el bloque `anchoring` (capa 2). 2 añadió `duplicated_fields` al informe del camino de CIMA.
Se sube aunque las claves sean aditivas porque sin la versión no se puede distinguir «no se detectó
nada» de «este informe se escribió antes de que se mirara», y los informes son append-only: los de
versiones anteriores se quedan como están. El camino de CIMA no lleva `anchoring` ni `salient` —la
ficha se verifica contra el registro oficial, no contra un Markdown que no existe—, así que su
ausencia en un informe de ficha no es una carencia.
"""

_SHORT_HEADING_CHARS = 2


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    fusionados: list[tuple[int, int]] = []
    for inicio, fin in sorted(spans):
        if fusionados and inicio <= fusionados[-1][1]:
            previo = fusionados[-1]
            fusionados[-1] = (previo[0], max(previo[1], fin))
        else:
            fusionados.append((inicio, fin))
    return fusionados


def _covered(spans: list[tuple[int, int]]) -> int:
    return sum(fin - inicio for inicio, fin in spans)


def _ratio(parte: int, total: int) -> float:
    return round(parte / total, 4) if total else 0.0


def conversion_quality(doc_sections: list[DocumentSection]) -> dict[str, Any]:
    """Capa 0: señales de que el conversor destrozó la estructura del documento.

    Los encabezados de ≤2 caracteres son las capitulares del PDF que el conversor emite como
    encabezado (un informe del corpus tenía 3 de 14); la proporción de `OTHER_CLINICAL` dice
    cuánto documento quedó sin clasificar, que es de donde viene la inanición de los extractos.
    Las pseudo-secciones de nivel 0 (preámbulo, documento sin encabezados) no son encabezados
    del conversor y quedan fuera del recuento de capitulares.
    """
    headings = [s for s in doc_sections if s.level > 0]
    cortos = sum(1 for s in headings if len(s.heading.strip()) <= _SHORT_HEADING_CHARS)
    other = sum(1 for s in doc_sections if s.category == SectionCategory.OTHER_CLINICAL)
    return {
        "headings_total": len(headings),
        "short_headings": cortos,
        "short_heading_ratio": _ratio(cortos, len(headings)),
        "sections_total": len(doc_sections),
        "other_clinical_sections": other,
        "other_clinical_ratio": _ratio(other, len(doc_sections)),
        "has_structure": document_has_structure(doc_sections),
    }


def input_coverage(doc_chars: int, chunks: dict[str, SectionChunk]) -> dict[str, Any]:
    """Capa 1: cuánto documento vio cada sección y cuánto no vio ninguna.

    Son dos métricas distintas a propósito: en el fallo de las 14 recomendaciones la unión
    global era del 100% —otras secciones recibían el documento entero— mientras la sección que
    perdía contenido recibía el 14%. Solo la cobertura por sección lo detecta.
    """
    secciones: dict[str, Any] = {}
    todos: list[tuple[int, int]] = []
    for key, chunk in sorted(chunks.items()):
        fusionados = _merged(list(chunk.covered_spans))
        cubiertos = _covered(fusionados)
        secciones[key] = {
            "chunk_chars": len(chunk.text),
            "covered_doc_chars": cubiertos,
            "coverage": _ratio(cubiertos, doc_chars),
            "truncated_chars": chunk.truncated_chars,
        }
        todos.extend(fusionados)
    cubiertos_global = _covered(_merged(todos))
    return {
        "global_covered_chars": cubiertos_global,
        "global_coverage": _ratio(cubiertos_global, doc_chars),
        "uncovered_chars": doc_chars - cubiertos_global,
        "sections": secciones,
    }


_NUMBER_RE = re.compile(r"\d(?:[.,   ]?\d)+|\d")
_NUM_DATE_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\b")
_DIGITS_RE = re.compile(r"\d+")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_MIN_TERM_LEN = 4
_SNIPPET_CHARS = 160

_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
"""Solo nombres de mes completos, no abreviaturas.

`jan`, `mar`, `may`, `jun`… casan con prosa corriente bajo `re.IGNORECASE` (`mar` es «mar», `may`
el verbo inglés), así que un fragmento como `may 5, 2024` se tomaría por fecha. El año de una
fecha ya lo ancla la comprobación de número, así que perder las fechas con mes abreviado cuesta
poco frente al falso positivo que introducirían, el riesgo principal declarado del plan.
"""
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_DATE_RES = (
    re.compile(rf"\b(\d{{1,2}})\s+de\s+({_MONTH_ALT})\s+de\s+(\d{{4}})\b", re.IGNORECASE),
    re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})\b", re.IGNORECASE),
    re.compile(rf"\b({_MONTH_ALT})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE),
)


def _normalize_number(literal: str) -> str:
    """Deja solo los dígitos: así `44,467`, `44.467` y `44 467` anclan contra `44467`.

    El anclaje pregunta si la secuencia de dígitos existe en el fuente, no si el separador
    coincide, que es lo que dio cero falsos positivos sobre 15 documentos. Sobre-normalizar
    —perder la distinción entre coma decimal y de millares— hace el emparejado más indulgente,
    que es la dirección correcta para una capa que solo informa: un falso positivo es peor que
    un falso negativo cuando el riesgo declarado del plan es bloquear un documento bueno.
    """
    return re.sub(r"\D", "", literal)


def _number_key(literal: str) -> str:
    """Clave de anclaje de un número: dígitos sin ceros a la izquierda.

    `0.001` y `.001` —la misma cifra, con y sin el cero que la convención estadística médica
    omite— comparten clave `1`; sin quitar los ceros, `0001` y `001` no anclarían y la cifra
    saldría como fabricada. Es el mismo arreglo que necesita un código nacional al que Excel le
    come el cero inicial. A cambio se pierde la distinción entre `0.5` y `5`, un falso negativo
    asumible en una capa cuyo riesgo principal es el falso positivo.
    """
    return _normalize_number(literal).lstrip("0") or "0"


def _date_key(literal: str) -> tuple[int, ...]:
    """Los tres números de una fecha, ordenados, para anclar sin depender del formato.

    Una fecha del JSON como `2024-03-12` debe anclar contra `12/03/2024` del fuente: ordenar los
    componentes hace el emparejado insensible al orden y al locale (el corpus es español e inglés).
    Es más indulgente que fijar día/mes/año, y a propósito: fabricar una fecha que comparta los
    tres números con una real del documento es raro, y el falso positivo es el riesgo a evitar.
    """
    return tuple(sorted(int(g) for g in _DIGITS_RE.findall(literal)))


def _iter_dates(text: str) -> Iterator[tuple[str, tuple[int, ...]]]:
    """Fechas del texto como `(literal, clave)`, en formato numérico y con mes en palabra.

    El mes en palabra es bilingüe (`12 de marzo de 2024`, `March 12, 2024`) y su clave es la
    misma que la de la fecha numérica equivalente, así que anclan entre sí. Se exige día, mes y
    año: un `marzo de 2024` suelto no se toma por fecha —su año ya lo ancla la comprobación de
    número— para no inventar coincidencias, que es el falso positivo que el plan evita.
    """
    for m in _NUM_DATE_RE.finditer(text):
        yield m.group(0), _date_key(m.group(0))
    for rx in _MONTH_DATE_RES:
        for m in rx.finditer(text):
            numeros = [int(g) for g in m.groups() if g.isdigit()]
            mes = next(_MONTHS[g.casefold()] for g in m.groups() if not g.isdigit())
            yield m.group(0), tuple(sorted([*numeros, mes]))


def _content_terms(text: str) -> list[str]:
    """Palabras alfabéticas de `_MIN_TERM_LEN` o más, en minúsculas y sin números.

    Los números los ancla su propia normalización; aquí son ruido. El umbral de longitud descarta
    conectores y siglas cortas sin una lista de stopwords por idioma, que funciona igual en
    español y en inglés.
    """
    return [t.casefold() for t in _WORD_RE.findall(text) if len(t) >= _MIN_TERM_LEN]


def _normalize_ws(text: str) -> str:
    return " ".join(text.casefold().split())


def _snippet(text: str) -> str:
    colapsado = " ".join(text.split())
    if len(colapsado) <= _SNIPPET_CHARS:
        return colapsado
    return colapsado[:_SNIPPET_CHARS] + "…"


def _document_provenance_values(
    nodo: Any, variant: DocVariant, ruta: str = ""
) -> Iterator[tuple[str, str]]:
    """Devuelve `(ruta, texto)` por cada hoja cuya procedencia es el documento.

    Solo esas se pueden anclar contra el Markdown (precondición 1 del plan): un número que solo
    aparezca en `reimbursement` —que viene de BiFiMed— o en una ficha de CIMA no tiene por qué
    estar en el texto. Las claves privadas del motor (todas las que empiezan por `_`) se saltan
    por prefijo. Los valores numéricos por tipo (un `int`/`float`) también cuentan; `bool`
    no, que en Python es subclase de `int` y no es un dato del documento.
    """
    if isinstance(nodo, bool):
        return
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if not clave.startswith("_"):
                yield from _document_provenance_values(
                    valor, variant, f"{ruta}.{clave}" if ruta else clave
                )
    elif isinstance(nodo, list):
        for valor in nodo:
            yield from _document_provenance_values(valor, variant, ruta)
    elif isinstance(nodo, (str, int, float)) and field_provenance(variant, ruta) == "document":
        yield ruta, str(nodo)


def _anchor_numbers(leaves: list[tuple[str, str]], md_text: str) -> dict[str, Any]:
    source = {_number_key(m) for m in _NUMBER_RE.findall(md_text)}
    unanchored: list[dict[str, str]] = []
    total = 0
    for ruta, valor in leaves:
        for numero in _NUMBER_RE.findall(valor):
            total += 1
            if _number_key(numero) not in source:
                unanchored.append({"field": ruta, "value": numero})
    return {
        "numbers_total": total,
        "numbers_unanchored": len(unanchored),
        "unanchored_numbers": unanchored,
    }


def _anchor_dates(leaves: list[tuple[str, str]], md_text: str) -> dict[str, Any]:
    source = {clave for _, clave in _iter_dates(md_text)}
    unanchored: list[dict[str, str]] = []
    total = 0
    for ruta, valor in leaves:
        for literal, clave in _iter_dates(valor):
            total += 1
            if clave not in source:
                unanchored.append({"field": ruta, "value": literal})
    return {
        "dates_total": total,
        "dates_unanchored": len(unanchored),
        "unanchored_dates": unanchored,
    }


def _anchor_text(
    leaves: list[tuple[str, str]], md_text: str, variant: DocVariant
) -> dict[str, Any]:
    source_terms = set(_content_terms(md_text))
    source_norm = _normalize_ws(md_text)
    verbatim = verbatim_fields(variant)
    text_checked = 0
    verbatim_checked = 0
    unanchored_text: list[dict[str, str]] = []
    unanchored_verbatim: list[dict[str, str]] = []
    for ruta, valor in leaves:
        if ruta in verbatim:
            verbatim_checked += 1
            if _normalize_ws(valor) not in source_norm:
                unanchored_verbatim.append({"field": ruta, "value": _snippet(valor)})
            continue
        terminos = _content_terms(valor)
        if terminos:
            text_checked += 1
            if not any(t in source_terms for t in terminos):
                unanchored_text.append({"field": ruta, "value": _snippet(valor)})
    return {
        "text_checked": text_checked,
        "text_unanchored": len(unanchored_text),
        "unanchored_text": unanchored_text,
        "verbatim_checked": verbatim_checked,
        "verbatim_unanchored": len(unanchored_verbatim),
        "unanchored_verbatim": unanchored_verbatim,
    }


def output_anchoring(
    md_text: str, clinical_json: dict[str, Any], variant: DocVariant
) -> dict[str, Any]:
    """Capa 2: ancla la salida contra el Markdown fuente, sin dictaminar.

    Cuatro comprobaciones sobre los campos de procedencia-documento, con los literales sin anclar
    y su ruta para que el dashboard los ordene y la fase 4 mida el corte contra el suelo de ruido:

    - **números** y **fechas** (numéricas y con mes en palabra): la secuencia debe existir en el
      fuente, normalizando separadores y orden. Son independientes a propósito —el número ancla
      cada componente y la fecha la combinación—, así que `numbers_total` cuenta también los
      dígitos de una fecha.
    - **verbatim por contrato** (`verbatim_fields`): nombres de fármaco, biomarcador y escala, que
      el prompt copia exactos; se exige subcadena. Parafrasearlos es un defecto.
    - **paráfrasis** (el resto): basta con que algún término raro del valor aparezca en el fuente.
      Es la técnica con que se validaron las 14 recomendaciones recuperadas, e indulgente porque
      el falso positivo es el riesgo principal declarado del plan.
    """
    leaves = list(_document_provenance_values(clinical_json, variant, ""))
    return {
        "checked_fields": len(leaves),
        **_anchor_numbers(leaves, md_text),
        **_anchor_dates(leaves, md_text),
        **_anchor_text(leaves, md_text, variant),
    }


_SALIENT_TERMS_PER_SECTION = 12
_BOILERPLATE_HEADING_RE = re.compile(
    r"(?<!and )\b(references|referencias|bibliograf[ií]a|bibliography)(\s*\([^)]*\))?\W*$"
    r"|^\W*(\d+(\.\d+)*\.?\s+)?(reference|referencias\s+bibliogr[aá]ficas|reference\s+list)\W*$"
    r"|^\W*(\d+(\.\d+)*\.?\s+)?(references|referencias)\s+[^\W\d_]"
    r"|\blists?\s+of\s+references\b"
    r"|^\W*(\d+(\.\d+)*\.?\s+)?(disclosures?|financiaci[oó]n"
    r"|funding(\s+(information|statement|sources?))?)\W*$"
    r"|\b((role|sources?)\s+of\s+(the\s+)?funding|guideline\s+funding)"
    r"|\b(acknowledg\w*|agradecimientos?|competing\s+interests?|conflicts?[\s-]+of[\s-]+int"
    r"|conflictos?\s+de\s+(\w+\s+){0,3}inter|declaraci[oó]n\s+de\s+inter)",
    re.IGNORECASE,
)


def _document_provenance_terms(clinical_json: dict[str, Any], variant: DocVariant) -> set[str]:
    terminos: set[str] = set()
    for _ruta, valor in _document_provenance_values(clinical_json, variant, ""):
        terminos.update(_content_terms(valor))
    return terminos


def salient_recall(
    clinical_json: dict[str, Any],
    doc_sections: list[DocumentSection],
    variant: DocVariant,
) -> dict[str, Any]:
    presentes = _document_provenance_terms(clinical_json, variant)
    frecuencia: Counter[str] = Counter()
    for seccion in doc_sections:
        frecuencia.update(set(_content_terms(seccion.content)))

    secciones: dict[str, Any] = {}
    omitidas: list[str] = []
    for indice, seccion in enumerate(doc_sections):
        if _BOILERPLATE_HEADING_RE.search(seccion.heading):
            continue
        terminos = set(_content_terms(seccion.content))
        if not terminos:
            continue
        salientes = sorted(terminos, key=lambda t: (frecuencia[t], t))[:_SALIENT_TERMS_PER_SECTION]
        recuperados = [t for t in salientes if t in presentes]
        clave = f"{indice}:{seccion.heading.strip()[:60]}"
        secciones[clave] = {
            "category": str(seccion.category),
            "salient_terms": len(salientes),
            "present": len(recuperados),
            "recall": _ratio(len(recuperados), len(salientes)),
        }
        if not recuperados:
            omitidas.append(clave)

    return {
        "salient_terms_per_section": _SALIENT_TERMS_PER_SECTION,
        "sections": secciones,
        "omitted_sections": omitidas,
    }


def build_cima_fidelity_report(
    nregistro: str,
    doc_chars: int,
    campos: set[str],
    esperados: set[str],
    cubierto: int,
    descartadas: list[dict[str, Any]],
    duplicados: dict[str, int],
) -> dict[str, Any]:
    """Capa 1 para la ficha construida desde CIMA, donde no hay Markdown ni troceado.

    La capa 0 no aplica —no hay conversión que medir— pero **la 1 sí, y no como formalidad**: el
    JSON de CIMA trae todas las secciones, así que la fracción de su texto que acaba en algún campo
    del esquema es una medida real. Sobre las cinco fichas de referencia da 0,897 a 0,986. No se
    finge un 100%: eso pondría las fichas al final de una lista ordenada por la peor sección, que
    es justo donde no deben estar.

    La cobertura por sección es binaria por la naturaleza de la fuente: el campo tiene contenido o
    no lo tiene. Un campo vacío deja su cobertura en 0 y con ello el mínimo del documento, que es
    lo que el dashboard ordena. Son 6 de 500 fichas censadas, con `pregnancy_and_lactation` en 3,
    `indications` en 2 y `container` en 1.

    Se mira el campo ya construido y no la sección de la que salió, porque tres campos los puede
    rellenar el metadato estructurado cuando su sección falta —el número de registro, el titular y
    la forma farmacéutica—. Contarlos como ausentes sería un falso positivo, que es el riesgo que
    el plan del revisor nombra como principal.

    `duplicated_fields` nombra los campos compuestos de partes que se contienen entre sí, con los
    caracteres duplicados. Un campo lo alimentan su sección y sus subsecciones, y en el JSON de CIMA
    cada una trae solo su texto, así que hoy no se dispara: es el detector de que CIMA vuelva a
    anidar, porque cuando un campo se compuso de partes anidadas (en el script que construyó el
    corpus congelado) el resultado fue texto archivado dos veces sin ningún error. Solo lo declara
    el camino de CIMA; en el del PDF la clave falta, y las plantillas lo pintan solo si trae algo.

    `discarded_sections` nombra lo que se queda fuera del esquema con su tamaño, en vez de
    perderlo en silencio: siempre `5.3`, los datos preclínicos de seguridad, que están en las 1.000
    fichas del segundo censo y no tienen campo donde caer, más las dos fechas finales.

    El informe lleva el **número de registro** porque varios pueden colgar del mismo documento: sin
    él, dos filas de `/fidelity/` serían indistinguibles y la peor podría pertenecer a un alias.

    La forma es la misma que la del camino del PDF, con `conversion` a `None` y los contadores por
    sección a cero, porque **las dos plantillas la renderizan sin preguntar por el camino**: sin las
    claves, Django pintaba huecos con el `string_if_invalid` vacío y una ficha parecía un documento
    medido en cero en vez de uno donde la capa 0 no aplica. `uncovered_chars` no puede quedar en
    negativo aunque las secciones sumaran más que el documento.
    """
    return {
        "report_version": REPORT_VERSION,
        "variant": "ficha_tecnica",
        "path": "cima",
        "registration_number": nregistro,
        "doc_chars": doc_chars,
        "conversion": None,
        "coverage": {
            "global_covered_chars": cubierto,
            "global_coverage": _ratio(cubierto, doc_chars),
            "uncovered_chars": max(0, doc_chars - cubierto),
            "sections": {
                campo: {
                    "coverage": 1.0 if campo in campos else 0.0,
                    "chunk_chars": 0,
                    "covered_doc_chars": 0,
                    "truncated_chars": 0,
                }
                for campo in sorted(esperados)
            },
        },
        "missing_fields": sorted(esperados - campos),
        "duplicated_fields": duplicados,
        "discarded_sections": descartadas,
    }


def build_fidelity_report(
    variant: DocVariant,
    md_text: str,
    doc_sections: list[DocumentSection],
    chunks: dict[str, SectionChunk],
) -> dict[str, Any]:
    """Informe completo de la fase 2, versionado para que el consumidor sepa qué lee.

    El informe vive fuera del JSON clínico (modelo propio, decisión del plan): aquí solo se
    construye; persistirlo y enseñarlo es de docsrepo.
    """
    return {
        "report_version": REPORT_VERSION,
        "variant": variant,
        "doc_chars": len(md_text),
        "conversion": conversion_quality(doc_sections),
        "coverage": input_coverage(len(md_text), chunks),
    }