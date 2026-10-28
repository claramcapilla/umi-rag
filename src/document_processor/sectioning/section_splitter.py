"""Divide Markdown limpio en secciones clínicas clasificadas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from src.document_processor.config import HEADING_RE as _HEADING_RE


class SectionCategory(StrEnum):
    """Categorías clínicas de secciones de un documento.

    `StrEnum` en lugar de `(str, Enum)`: sus miembros siguen comparando con la cadena, pero
    `str(miembro)` devuelve el valor y no `"SectionCategory.X"`. Nada del pipeline depende de
    esa representación —las categorías solo se usan como claves y en pertenencia a conjuntos—
    y así no hace falta suprimir UP042.
    """

    TITLE_META = "title_meta"
    ABSTRACT = "abstract"
    INTRODUCTION = "introduction"
    METHODS = "methods"
    RESULTS = "results"
    DISCUSSION = "discussion"
    CONCLUSION = "conclusion"
    SAFETY = "safety"
    OTHER_CLINICAL = "other_clinical"


_CLASSIFICATION_RULES: list[tuple[re.Pattern[str], SectionCategory]] = [
    (
        re.compile(
            r"(^abstract|^resumen|^summary|"
            r"^zusammenfassung|highlights|"
            r"^key\s+points?|"
            r"^what\s+is\s+(already\s+)?known|"
            r"^what\s+this|"
            r"^graphical\s+abstract)",
            re.IGNORECASE,
        ),
        SectionCategory.ABSTRACT,
    ),
    (
        re.compile(
            r"(introduc|background|grundlagen|"
            r"antecedentes|"
            r"acerca\s+del?\s|"
            r"^definition|"
            r"incidence.+epidemiol|"
            r"epidemiolog|"
            r"^scope$|^preamble$|"
            r"^rationale$|"
            r"^general\s+information$|"
            r"^justificaci[oó]n$|"
            r"^aims?$|^objectives?$|^objetivos?$)",
            re.IGNORECASE,
        ),
        SectionCategory.INTRODUCTION,
    ),
    (
        re.compile(
            r"(method|m[eé]todo|design|dise[ñn]o|"
            r"patients?\s+(and|selection|character)|"
            r"poblaci[oó]n|study\s+population|"
            r"statistical|estad[ií]stic|"
            r"patient\s+stratif|"
            r"posolog[ií]a|dosificaci|"
            r"forma\s+farmac[eé]utica|"
            r"forma\s+de\s+administraci|"
            r"composici[oó]n\s+cualitativa|"
            r"indicaciones?\s+terap|"
            r"diagn[oó]s|patholog|"
            r"molecular\s+biolog|"
            r"staging|estadificaci|"
            r"risk\s+assess|risk\s+stratif|"
            r"clasificaci[oó]n|classif|"
            r"pruebas?\s+(para|m[eé]dic)|"
            r"biopsia|an[aá]lisis\s+de\s+sangre|"
            r"^absorci[oó]n$|^distribuci[oó]n$|"
            r"^metabolismo$|^eliminaci[oó]n$|"
            r"^biotransformaci|"
            r"mecanismo\s+de\s+acci[oó]n|"
            r"mechanism\s+of\s+action|"
            r"ecocardiogra|echocardiogra|"
            r"catheteris|cateterismo|"
            r"^screening$|"
            r"^clinical\s+information$|"
            r"^eligib|^inclusion|^exclusion|"
            r"^material|"
            r"data\s+(collection|extraction|analysis)|"
            r"search\s+strateg|"
            r"quality\s+assess|"
            r"risk\s+of\s+bias|"
            r"^PRISMA$|"
            r"variables?\s+collected)",
            re.IGNORECASE,
        ),
        SectionCategory.METHODS,
    ),
    (
        re.compile(
            r"(safety|seguridad|"
            r"reacciones?\s+adversas|"
            r"adverse\s+(event|reaction)|"
            r"toxicidad|toxicit|"
            r"contraindicacion|contraindication|"
            r"advertencias?\s+y\s+precaucion|"
            r"warnings?\s+and\s+precaution|"
            r"interacci[oó]n.+medicamento|"
            r"drug\s+interaction|"
            r"sobredosis|overdose|"
            r"profilaxis|prophylax|"
            r"manejo\s+de\s+(infeccion|citopen)|"
            r"s[ií]ndrome\s+de\s+(liberaci|lisis)|"
            r"^efectos?\s+secundarios?$|"
            r"^side\s+effects?$|"
            r"^tolerab|^discontinu|"
            r"^citopenia|^infecci[oó]n|^infection|"
            r"^hipersensibilidad|hypersensitiv|"
            r"acontecimientos?\s+(hemorrág|"
            r"hep[aá]t|cerebrovascul|card[ií]ac)|"
            r"arritmias?\s+card[ií]ac|"
            r"^hipertensi[oó]n$|"
            r"^embarazo|^pregnancy|"
            r"^lactancia|^breastfeed|"
            r"^fertilidad|^fertility|"
            r"fertilidad.*embarazo.*lactancia|"
            r"mujeres?\s+en\s+edad\s+f[eé]rtil)",
            re.IGNORECASE,
        ),
        SectionCategory.SAFETY,
    ),
    (
        re.compile(
            r"(results?|resultado|efficacy|eficacia|"
            r"outcomes?|desenlace|"
            r"response|respuesta|"
            r"survival|supervivencia|"
            r"primary\s+(endpoint|outcome)|"
            r"secondary\s+(endpoint|outcome)|"
            r"propiedades?\s+farmacol[oó]g|"
            r"datos\s+cl[ií]nicos|"
            r"farmacod|pharmacod|"
            r"farmacocin|pharmacokin|"
            r"disease\s+management|"
            r"treatment|tratamiento|therap|"
            r"first.?line|second.?line|"
            r"r/r\s|relapse|recidiva|refractar|"
            r"^findings?$|"
            r"^demographics?$|^baseline|"
            r"^subgroup|sensitivity\s+analys|"
            r"^prevalence$|^incidence$|"
            r"clinical\s+characteristics$|"
            r"^monoterapia$|^monotherapy$|"
            r"^calidad\s+de\s+vida|"
            r"quality\s+of\s+life)",
            re.IGNORECASE,
        ),
        SectionCategory.RESULTS,
    ),
    (
        re.compile(
            r"(discussion|discusi[oó]n|"
            r"interpretation|interpretaci|"
            r"implications?|implicacion|"
            r"limitaci|limitation|"
            r"^strengths?$|^weakness|"
            r"^comparison\s+with|"
            r"^fortalezas?$|^debilidades?$)",
            re.IGNORECASE,
        ),
        SectionCategory.DISCUSSION,
    ),
    (
        re.compile(
            r"(conclusi|bottom\s+line|"
            r"key\s+(message|finding|point)|"
            r"^recomendacion|^recommendation|"
            r"clinical\s+impact|"
            r"impacto\s+cl[ií]nico|"
            r"follow.?up|seguimiento|"
            r"^future\s+(direction|research|stud)|"
            r"direcciones?\s+futuras|"
            r"^clinical\s+significance$|"
            r"^practice\s+points?$|"
            r"gaps?\s+in\s+evidence|"
            r"quality\s+indicator)",
            re.IGNORECASE,
        ),
        SectionCategory.CONCLUSION,
    ),
]


@dataclass
class DocumentSection:
    """Una sección del documento con su clasificación."""

    heading: str
    level: int
    content: str
    category: SectionCategory
    char_offset: int
    char_length: int

    @property
    def full_text(self) -> str:
        """Heading + contenido completo."""
        prefix = "#" * self.level
        return f"{prefix} {self.heading}\n\n{self.content}"


def _normalize_heading(text: str) -> str:
    """Normaliza un heading antes de clasificarlo.

    Corrige artefactos del extractor PDF y quita prefijos de
    numeración para que las reglas de clasificación
    puedan hacer match limpio.
    """
    clean = text.strip()

    if re.match(r"^[A-Z](\s[A-Z]){2,}$", clean):
        clean = clean.replace(" ", "")

    clean = re.sub(r"^[A-Z]\.\s+", "", clean)

    clean = re.sub(r"^[IVXLC]+\.\s+", "", clean)

    clean = re.sub(r"^\d+[\.\d]*\s*\.?\s*", "", clean)

    return clean


def _classify_heading(text: str) -> SectionCategory:
    """Clasifica un heading en una categoría clínica."""
    clean = _normalize_heading(text)

    for pattern, category in _CLASSIFICATION_RULES:
        if pattern.search(clean):
            return category

    return SectionCategory.OTHER_CLINICAL


def split_document(
    md_text: str,
) -> list[DocumentSection]:
    """Divide Markdown en secciones clasificadas.

    Args:
        md_text: Markdown limpio (ya procesado por
                 clean_markdown).

    Returns:
        Lista de DocumentSection ordenadas por posición.
    """
    sections: list[DocumentSection] = []

    matches = list(_HEADING_RE.finditer(md_text))

    if not matches:
        sections.append(
            DocumentSection(
                heading="(documento completo)",
                level=0,
                content=md_text,
                category=SectionCategory.OTHER_CLINICAL,
                char_offset=0,
                char_length=len(md_text),
            )
        )
        return sections

    first_pos = matches[0].start()
    if first_pos > 0:
        preamble = md_text[:first_pos].strip()
        if preamble:
            sections.append(
                DocumentSection(
                    heading="(preámbulo)",
                    level=0,
                    content=preamble,
                    category=SectionCategory.TITLE_META,
                    char_offset=0,
                    char_length=first_pos,
                )
            )

    for i, match in enumerate(matches):
        level = len(match.group(1))
        heading_text = match.group(2).strip()

        content_start = match.end()
        if i + 1 < len(matches):
            content_end = matches[i + 1].start()
        else:
            content_end = len(md_text)

        content = md_text[content_start:content_end].strip()
        category = _classify_heading(heading_text)

        sections.append(
            DocumentSection(
                heading=heading_text,
                level=level,
                content=content,
                category=category,
                char_offset=match.start(),
                char_length=content_end - match.start(),
            )
        )

    _propagate_parent_categories(sections)

    return sections


def _propagate_parent_categories(
    sections: list[DocumentSection],
) -> None:
    """Herencia jerárquica de categorías.

    Para cada sección OTHER_CLINICAL cuyo level es mayor
    que el de la sección clasificada anterior, hereda la
    categoría del padre. Esto permite que sub-headings
    sin clasificación propia (ej: h3 bajo h2=RESULTS)
    se incluyan en los chunks correctos.

    Modifica la lista in-place.
    """
    parent_stack: list[tuple[int, SectionCategory]] = []

    for sec in sections:
        while parent_stack and parent_stack[-1][0] >= sec.level:
            parent_stack.pop()

        if sec.category != SectionCategory.OTHER_CLINICAL:
            parent_stack.append((sec.level, sec.category))
        elif parent_stack:
            sec.category = parent_stack[-1][1]
            parent_stack.append((sec.level, sec.category))


def get_sections_by_category(
    sections: list[DocumentSection],
    *categories: SectionCategory,
) -> list[DocumentSection]:
    """Filtra secciones por una o más categorías."""
    cat_set = set(categories)
    return [s for s in sections if s.category in cat_set]


def document_has_structure(
    sections: list[DocumentSection],
) -> bool:
    """True si el documento tiene estructura reconocible.

    Un documento 'tiene estructura' si al menos 2
    categorías clínicas distintas fueron identificadas
    (excluyendo OTHER_CLINICAL y TITLE_META).
    """
    clinical_cats = {
        s.category
        for s in sections
        if s.category
        not in (
            SectionCategory.OTHER_CLINICAL,
            SectionCategory.TITLE_META,
        )
    }
    return len(clinical_cats) >= 2