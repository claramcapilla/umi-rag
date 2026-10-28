"""Detecta el tipo de documento clínico.

Clasifica como ficha técnica, guía clínica o artículo.

Los marcadores de ficha técnica (SmPC) se anclan al **inicio de línea** (posición de encabezado):
en una ficha real son títulos de sección numerados por su normativa, no prosa. Sin el ancla, un
paper o una guía que mencione «ficha técnica», «datos clínicos» o «propiedades farmacológicas» de
pasada sumaba puntos y llegaba al umbral: una guía de colposcopia de 227 kB alcanzó 3 por tres
menciones en medio de frase y quedó rechazada como SmPC (`FichaTecnicaNoIngerible`). Anclar solo
puede quitar coincidencias de prosa, nunca crear nuevas, así que reduce falsos positivos sin
debilitar la detección de una ficha real, cuyos marcadores siguen viviendo en encabezado.
"""

from __future__ import annotations

import re

from src.document_processor.config import (
    DETECTOR_GUIDELINE_THRESHOLD,
    DETECTOR_SMPC_THRESHOLD,
    ROUTER_CLASSIFY_CHARS,
)
from src.document_processor.schema import DocVariant

_SMPC_HEADING = r"^[ \t]*#{0,6}[ \t]*"
_SMPC_HEADING_NUMBERED = _SMPC_HEADING + r"(?:\d+[.)]?[ \t]+)?"

_SMPC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(_SMPC_HEADING_NUMBERED + core, re.IGNORECASE | re.MULTILINE)
    for core in [
        r"FICHA\s+T[EÉ]CNICA",
        r"NOMBRE\s+DEL\s+MEDICAMENTO",
        r"COMPOSICI[OÓ]N\s+CUALITATIVA",
        r"DATOS\s+CL[IÍ]NICOS",
        r"PROPIEDADES\s+FARMACOL[OÓ]GICAS",
        r"DATOS\s+FARMAC[EÉ]UTICOS",
    ]
] + [
    re.compile(_SMPC_HEADING + core, re.IGNORECASE | re.MULTILINE)
    for core in [
        r"4\.\s*1\b.*(?:indicacion|terap[eé]utica)",
        r"4\.\s*2\b.*(?:posolog[ií]a|forma\s+de\s+administraci)",
        r"4\.\s*3\b.*contraindicacion",
        r"4\.\s*8\b.*(?:reacciones?\s+adversas|adverse)",
        r"5\.\s*1\b.*farmacod",
        r"6\.\s*1\b.*(?:excipiente|lista\s+de\s+excipiente)",
    ]
]

_SMPC_THRESHOLD = DETECTOR_SMPC_THRESHOLD

_GUIDELINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"GU[IÍ]A\s+DE\s+PR[AÁ]CTICA\s+CL[IÍ]NICA",
        r"CLINICAL\s+PRACTICE\s+GUIDELINE",
        r"CONSENSUS\s+STATEMENT",
        r"\bNCCN\b",
        r"\bESMO\b",
        r"\bGELTAMO\b",
        r"\bGESIDA\b",
        r"\bBSH\b",
        r"\bASH\b",
        r"nivel\s+de\s+evidencia",
        r"grado\s+de\s+recomendaci[oó]n",
        r"level\s+of\s+evidence",
        r"strength\s+of\s+recommendation",
        r"algoritmo\s+de\s+tratamiento",
        r"treatment\s+algorithm",
        r"primera\s+l[ií]nea",
        r"first[\s\-\.]+line",
        r"segunda\s+l[ií]nea",
        r"reca[ií]da[/\s]+refractari",
        r"estadificaci[oó]n",
        r"\bstaging\b",
        r"[ií]ndice\s+pron[oó]stico",
        r"prognostic\s+index",
    ]
]

_GUIDELINE_THRESHOLD = DETECTOR_GUIDELINE_THRESHOLD

_ARTICLE_STRUCTURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in [
        r"^#{1,3}\s+(?:abstract|resumen)\b",
        r"^#{1,3}\s+(?:methods?|m[eé]todos?)\b",
        r"^#{1,3}\s+(?:results?|resultados?)\b",
        r"^#{1,3}\s+(?:discussion|discusi[oó]n)\b",
        r"\b(?:original\s+article|case\s+report|"
        r"meta[\s\-]?analysis\s+of|systematic\s+review\s+of|"
        r"review\s+article|research\s+article|"
        r"brief\s+communication|letter\s+to\s+the\s+editor)\b",
    ]
]

_TITLE_GUIDELINE = re.compile(
    r"\b(guidelines?|gu[ií]as?|guidance|recommendations?|recomendaciones|consensus|consenso|"
    r"position (?:paper|statement)|statement|declaraci[oó]n|standards|"
    r"practice (?:bulletin|parameter)|committee opinion|clinical pathway|green-top)\b",
    re.IGNORECASE,
)
_TITLE_RESEARCH = re.compile(
    r"\b(review|revisi[oó]n|meta-?analys[ie]s|metaan[aá]lisis|systematic|trial|ensayo|cohort|"
    r"cohorte|case (?:report|series)|study|estudio)\b",
    re.IGNORECASE,
)
_TITLE_SCAN_LINES = 40
_TITLE_MAX_CHARS = 160


def detect_document_type(md_text: str) -> DocVariant:
    """Detecta si un documento es ficha técnica, guía clínica o artículo.

    Habla el vocabulario único del sistema (`DocVariant`), el mismo que el modelo, el chunker y
    el contrato con codigo-rojo. Antes devolvía `clinical_article`/`clinical_guideline` y hacía
    falta traducir, lo que dejaba dos nombres para cada concepto y un mapa por cada consumidor.

    Orden de detección:
    1. Ficha técnica (SmPC) — marcadores más distintivos
    2. Guía de práctica clínica (con penalización si
       tiene estructura de artículo)
    3. Artículo clínico (fallback)

    Args:
        md_text: Texto Markdown del documento.

    Returns:
        ``"ficha_tecnica"``, ``"guia_clinica"`` o ``"paper"``.
    """
    smpc_score = sum(1 for p in _SMPC_PATTERNS if p.search(md_text))
    if smpc_score >= _SMPC_THRESHOLD:
        return "ficha_tecnica"

    cg_score = sum(1 for p in _GUIDELINE_PATTERNS if p.search(md_text))
    article_score = sum(1 for p in _ARTICLE_STRUCTURE_PATTERNS if p.search(md_text))

    effective_cg = cg_score - article_score
    if effective_cg >= _GUIDELINE_THRESHOLD:
        return "guia_clinica"

    return "paper"


def _opening_title(md_text: str) -> str:
    lines = [
        line
        for line in md_text[:ROUTER_CLASSIFY_CHARS].splitlines()[:_TITLE_SCAN_LINES]
        if line.strip()
    ]
    for line in lines:
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return re.sub(r"^[-*]\s*\d*\s*", "", lines[0]).strip() if lines else ""


def _declared_type(title: str) -> str:
    guideline = bool(_TITLE_GUIDELINE.search(title))
    research = bool(_TITLE_RESEARCH.search(title))
    if guideline and research:
        return "both"
    if guideline:
        return "guideline"
    if research:
        return "research article or review"
    return "nothing"


def guideline_signal_digest(md_text: str) -> str:
    """Resumen de señales de guía calculadas sobre el documento COMPLETO, para el clasificador.

    El clasificador paper/guía solo ve la apertura del documento (`ROUTER_CLASSIFY_CHARS`), y las
    marcas que distinguen una guía —recomendaciones graduadas, niveles de evidencia, algoritmos de
    tratamiento— suelen vivir en el cuerpo, no en la portada. Este resumen las cuenta sobre todo el
    texto y se las da al modelo como evidencia, sin decidir por él: el enrutado sigue siendo del LLM
    (el regex solo, mide fatal —penaliza a las guías publicadas en revista por su estructura de
    artículo—, así que aquí es una pista, no un voto).

    Incluye a propósito la nota anti-trampa: un artículo que revisa o audita guías dispara muchas de
    estas marcas y aun así es un `paper`. Sin ella, el recuento empujaría esas revisiones a `guía`.
    """
    guia_presentes = [p for p in _GUIDELINE_PATTERNS if p.search(md_text)]
    cg_score = len(guia_presentes)
    article_score = sum(1 for p in _ARTICLE_STRUCTURE_PATTERNS if p.search(md_text))
    presentes = sorted({p.pattern for p in guia_presentes})
    title = _opening_title(md_text)
    return (
        "FULL-DOCUMENT SIGNALS (computed over the entire document, not just the opening):\n"
        f"- guideline-marker matches: {cg_score}\n"
        f"- research-article-structure matches: {article_score}\n"
        f"- guideline markers present: {', '.join(m[:40] for m in presentes[:12]) or 'none'}\n"
        "Weigh these as evidence of PURPOSE. A document that reviews, appraises or measures "
        "adherence to guidelines is a paper even if it triggers many guideline markers.\n"
        f"- the document's title ({title[:_TITLE_MAX_CHARS]!r}) declares it as: "
        f"{_declared_type(title)}"
    )