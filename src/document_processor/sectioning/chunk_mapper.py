"""Mapea cada sección del JSON de salida al trozo de Markdown que se manda al LLM.

El orden de las categorías en cada mapa define la prioridad de concatenación; `ABSTRACT`
aparece en casi todas porque es el contexto mínimo que orienta al modelo.

Cada trozo sale como `SectionChunk`, con su geometría sobre el documento original, porque el
texto solo no permite medir cobertura: un truncado sin registrar hacía contar como cubierta una
sección recortada, que es el aviso de implementación de la capa 1 del plan de fidelidad.
"""

from __future__ import annotations

from dataclasses import dataclass

from document_processor.config import (
    CHUNK_HEAD_CONTEXT_CHARS,
    CHUNK_MAX_CHARS,
    CHUNK_TAIL_CONTEXT_CHARS,
)
from document_processor.sections.documento_por_apartados import CLAVE_DOCUMENTO_POR_APARTADOS

from .section_splitter import (
    DocumentSection,
    SectionCategory,
    document_has_structure,
    get_sections_by_category,
)

SC = SectionCategory

SECTION_CHUNK_MAP: dict[str, list[SectionCategory]] = {
    "metadata": [
        SC.TITLE_META,
        SC.ABSTRACT,
    ],
    "background_and_objective": [
        SC.ABSTRACT,
        SC.INTRODUCTION,
    ],
    "methods": [
        SC.ABSTRACT,
        SC.METHODS,
    ],
    "results": [
        SC.ABSTRACT,
        SC.RESULTS,
        SC.SAFETY,
        SC.OTHER_CLINICAL,
    ],
    "discussion_and_implications": [
        SC.DISCUSSION,
        SC.CONCLUSION,
        SC.OTHER_CLINICAL,
    ],
    "clinical_entities": [
        SC.RESULTS,
        SC.METHODS,
        SC.INTRODUCTION,
        SC.SAFETY,
        SC.OTHER_CLINICAL,
    ],
    "tables_and_conclusions": [
        SC.RESULTS,
        SC.CONCLUSION,
        SC.ABSTRACT,
        SC.OTHER_CLINICAL,
    ],
    CLAVE_DOCUMENTO_POR_APARTADOS: [],
}

DEFAULT_MAX_CHARS = CHUNK_MAX_CHARS
_HEAD_CONTEXT_CHARS = CHUNK_HEAD_CONTEXT_CHARS
_TAIL_CONTEXT_CHARS = CHUNK_TAIL_CONTEXT_CHARS


@dataclass(frozen=True)
class SectionChunk:
    """Trozo enviado al LLM por una sección del JSON, con su geometría sobre el documento.

    `covered_spans` son intervalos `(inicio, fin)` de offsets sobre el Markdown limpio; pueden
    solaparse entre sí (el contexto de cabecera pisa las primeras secciones) y quien los consuma
    debe fusionarlos antes de sumar. `truncated_chars` es cuánto ensamblado quedó fuera por
    `max_chars`: sin él, una sección incluida pero recortada contaría como cubierta.

    El recorte de los spans en el punto de truncado es aproximado a granularidad de pieza, con
    una cota medida: la marca de la pieza de cola va **delante** de su texto, así que si el
    truncado cae dentro de ella sus 33 caracteres cuentan como visibles y el span declara hasta
    33 caracteres que no se enviaron. Afecta solo a las secciones con contexto de cola. En la de
    cabecera no pasa: allí la marca va detrás. Para ordenar sospechas basta y es determinista;
    ninguna capa lo usa como veredicto.
    """

    text: str
    covered_spans: tuple[tuple[int, int], ...]
    truncated_chars: int

    @classmethod
    def whole(cls, full_text: str) -> SectionChunk:
        """El documento entero sin recortar: cobertura total y nada truncado."""
        return cls(full_text, ((0, len(full_text)),), 0)


_Piece = tuple[str, tuple[int, int]]


def _assemble(pieces: list[_Piece], max_chars: int) -> SectionChunk:
    ensamblado = "\n\n".join(texto for texto, _ in pieces)
    spans: list[tuple[int, int]] = []
    pos = 0
    for texto, (inicio_doc, fin_doc) in pieces:
        visibles = min(len(texto), max_chars - pos)
        if visibles > 0:
            spans.append((inicio_doc, min(fin_doc, inicio_doc + visibles)))
        pos += len(texto) + 2
    return SectionChunk(
        text=_truncated(ensamblado, max_chars),
        covered_spans=tuple(spans),
        truncated_chars=max(len(ensamblado) - max_chars, 0),
    )


def _full_doc_piece(full_text: str) -> _Piece:
    return full_text, (0, len(full_text))


def _head_piece(full_text: str) -> _Piece:
    return _head_context(full_text), (0, min(len(full_text), _HEAD_CONTEXT_CHARS))


def _tail_piece(full_text: str) -> _Piece:
    return _tail_context(full_text), (max(len(full_text) - _TAIL_CONTEXT_CHARS, 0), len(full_text))


def _head_context(full_text: str) -> str:
    """Primeros N chars del documento como contexto."""
    if len(full_text) <= _HEAD_CONTEXT_CHARS:
        return full_text
    return full_text[:_HEAD_CONTEXT_CHARS] + "\n\n[... INICIO DEL DOCUMENTO ...]"


def _tail_context(full_text: str) -> str:
    """Últimos N chars del documento como contexto."""
    if len(full_text) <= _TAIL_CONTEXT_CHARS:
        return full_text
    return "\n\n[... FINAL DEL DOCUMENTO ...]\n\n" + full_text[-_TAIL_CONTEXT_CHARS:]


_TRUNCATED_MARK = "\n\n[CONTENIDO TRUNCADO]"

_NEEDS_HEAD_CONTEXT = frozenset({"metadata", "tables_and_conclusions"})
_NEEDS_TAIL_CONTEXT = frozenset({"tables_and_conclusions"})


def _truncated(text: str, max_chars: int) -> str:
    """Recorta dejando marca visible.

    La marca importa: un truncado silencioso haría creer al LLM que el documento acaba ahí.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_MARK


def _sections_pieces_deduped(
    doc_sections: list[DocumentSection],
    categories: list[SectionCategory],
) -> list[_Piece]:
    """Piezas (texto + span en el documento) de esas categorías, sin repetir.

    Una misma sección puede encajar en varias categorías pedidas; se deduplica por
    `char_offset`, que la identifica dentro del documento.
    """
    piezas: list[_Piece] = []
    vistos: set[int] = set()
    for categoria in categories:
        for seccion in get_sections_by_category(doc_sections, categoria):
            if seccion.char_offset in vistos:
                continue
            vistos.add(seccion.char_offset)
            span = (seccion.char_offset, seccion.char_offset + seccion.char_length)
            piezas.append((seccion.full_text, span))
    return piezas


_CG_STARVATION_GUARD = frozenset({"therapies_index", "safety"})
"""Secciones que caen al documento entero **solo si** su extracto sale famélico.

Las dos piden `results` y `safety` y son las que peor cobertura sacan del troceador. No van en
`_CG_FULL_DOC_SECTIONS` porque darles siempre el documento entero está medido y **empeora**
cuando el extracto ya era bueno: en una guía cuyo extracto era el 81%, el índice de terapias
cayó de 40 a 10 entradas y el bloque de seguridad de 56 a 37 hojas. La guarda solo se activa
donde la inanición está medida, que es donde también está medida la pérdida.

La lista es corta a propósito: aplicar el umbral a todas las secciones subía a documento
completo cosas que funcionaban bien con el 14-21% —justo el perfil que produjo esa regresión—
y el alcance no está medido.
"""

_FRACCION_MINIMA_DEL_DOCUMENTO = 0.30
"""Por debajo de esto, el extracto se considera famélico y se manda el documento entero.

Medido sobre las cinco guías del corpus: las secciones que perdían contenido comprobable
recibían el 8-11% del documento —un índice de terapias sin doce de las familias de fármacos
que la guía discute— mientras que las sanas rondaban el 81%. Entre ambos grupos no hay ningún
caso, así que el umbral solo tiene que caer en medio.

Es una calibración sobre cinco documentos y hay que revisarla con más corpus: está puesto en la
parte baja del hueco a propósito, porque el daño de diluir una sección sana está medido (40→10
terapias) y el de dejarla famélica también, y este umbral solo debe disparar en el segundo caso.
"""


def _build_chunk(
    section_key: str,
    doc_sections: list[DocumentSection],
    full_text: str,
    max_chars: int,
    *,
    section_map: dict[str, list[SectionCategory]],
    full_doc_sections: frozenset[str],
    head_context_sections: frozenset[str],
    tail_context_sections: frozenset[str],
    starvation_sections: frozenset[str] = frozenset(),
) -> SectionChunk:
    """Ensamblado común a las tres variantes: qué Markdown se le manda al LLM.

    Sin estructura reconocible va el documento completo, y con una clave que el mapa no
    conoce también: es preferible dar contexto de sobra que dejar la sección sin extraer.

    Si no se encontró ninguna sección, se manda solo la cabecera como contexto mínimo, en vez
    de volcar todo lo que se haya clasificado como `OTHER_CLINICAL`.
    """
    if section_key in full_doc_sections or not document_has_structure(doc_sections):
        return _assemble([_full_doc_piece(full_text)], max_chars)

    needed = section_map.get(section_key)
    if needed is None:
        return SectionChunk.whole(full_text)

    piezas = _sections_pieces_deduped(doc_sections, needed)

    if section_key in head_context_sections:
        piezas.insert(0, _head_piece(full_text))
    if section_key in tail_context_sections:
        piezas.append(_tail_piece(full_text))
    if not piezas:
        piezas.append(_head_piece(full_text))

    largo = sum(len(texto) for texto, _ in piezas) + 2 * (len(piezas) - 1)
    if (
        section_key in starvation_sections
        and largo < len(full_text) * _FRACCION_MINIMA_DEL_DOCUMENTO
    ):
        return _assemble([_full_doc_piece(full_text)], max_chars)

    return _assemble(piezas, max_chars)


def get_chunk_for_section(
    section_key: str,
    doc_sections: list[DocumentSection],
    full_text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> SectionChunk:
    """Chunk para una sección del JSON de un paper.

    `metadata` lleva siempre la cabecera del documento, porque título y autores suelen
    aparecer antes de cualquier encabezado reconocible. `tablas_y_conclusiones` lleva además
    la cola, para que el LLM vea principio y final.
    """
    return _build_chunk(
        section_key,
        doc_sections,
        full_text,
        max_chars,
        section_map=SECTION_CHUNK_MAP,
        full_doc_sections=frozenset({CLAVE_DOCUMENTO_POR_APARTADOS}),
        head_context_sections=_NEEDS_HEAD_CONTEXT,
        tail_context_sections=_NEEDS_TAIL_CONTEXT,
    )


SECTION_CHUNK_MAP_CG: dict[str, list[SectionCategory]] = {
    "metadata_and_scope": [
        SC.TITLE_META,
        SC.ABSTRACT,
        SC.INTRODUCTION,
    ],
    "conditions": [
        SC.INTRODUCTION,
        SC.METHODS,
        SC.RESULTS,
    ],
    "treatment_pathways": [
        SC.RESULTS,
        SC.SAFETY,
    ],
    "therapies_index": [
        SC.RESULTS,
        SC.SAFETY,
    ],
    "safety": [
        SC.SAFETY,
        SC.RESULTS,
    ],
    "recommendations": [
        SC.CONCLUSION,
        SC.RESULTS,
        SC.DISCUSSION,
    ],
    "artifacts": [],
    "special_populations_and_resources": [
        SC.INTRODUCTION,
        SC.DISCUSSION,
        SC.CONCLUSION,
        SC.OTHER_CLINICAL,
    ],
    CLAVE_DOCUMENTO_POR_APARTADOS: [],
}

_CG_FULL_DOC_SECTIONS = frozenset({"artifacts", "recommendations", CLAVE_DOCUMENTO_POR_APARTADOS})
"""Secciones que reciben la guía entera en vez de un extracto.

`recommendations` está aquí porque las recomendaciones de una guía **no viven en un sitio**:
aparecen al hilo de cada apartado, en cuadros de mensajes clave y en una tabla resumen al
final, así que cualquier extracto contiguo se deja fuera una parte. Medido sobre un informe
de grupo de trabajo de 87.358 caracteres: el extracto seleccionado eran 12.032 y no incluía
ni la tabla de recomendaciones ni nueve de las diez barreras, y salió **una** recomendación
de las diez que el documento enuncia. Es la sección de más valor de una guía para el RAG: es
donde menos se puede recortar la entrada.

`therapies_index` y `safety` NO están aquí, y se probó: ponerlas dio 29→51 terapias y 6→87
hojas de seguridad en la guía cuyo extracto era el 8% del documento, pero **40→10 y 56→37 en
otra cuyo extracto ya era el 81%**. Lo que hace daño no es el extracto, es que se quede
famélico; darle el documento entero a una sección que ya lo tenía casi todo solo diluye. Ese
caso lo cubre el umbral de inanición de abajo, que es la misma idea sin el efecto colateral.

Sustituye a un mecanismo que añadía secciones cuyo **contenido** casaba con un patrón
(«key messages», «summary of recommendations»). Funcionó para la guía que lo motivó y falló
con la siguiente, cuya tabla se titula «Workgroup recommendations»: perseguir redacciones de
encabezado es una carrera que se pierde documento a documento, y el fallo es silencioso —la
sección sale vacía o corta, sin error—.
"""

_CG_GENEROUS_SECTIONS = frozenset({"conditions", "treatment_pathways"})
_CG_HEAD_CONTEXT = _CG_GENEROUS_SECTIONS | {"metadata_and_scope"}


def get_chunk_for_section_cg(
    section_key: str,
    doc_sections: list[DocumentSection],
    full_text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> SectionChunk:
    """Chunk para una sección de guía clínica.

    `artifacts` necesita el documento entero: un algoritmo no vive en una sección concreta.
    `conditions` y `treatment_pathways` reciben cabecera y cola porque el alcance de una guía se
    reparte por todo el texto.
    """
    return _build_chunk(
        section_key,
        doc_sections,
        full_text,
        max_chars,
        section_map=SECTION_CHUNK_MAP_CG,
        full_doc_sections=_CG_FULL_DOC_SECTIONS,
        head_context_sections=_CG_HEAD_CONTEXT,
        tail_context_sections=_CG_GENEROUS_SECTIONS,
        starvation_sections=_CG_STARVATION_GUARD,
    )