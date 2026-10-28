"""Tipos del esquema clínico compartido con el RAG de codigo-rojo.

Los mapas `SECTIONS_*` de `sections/` venían anotados como `dict[str, dict]`, es decir sin
contrato: el valor podía ser cualquier cosa. `SectionSpec` lo fija, y es lo que consumen
tanto el chunker de indexado como el generador del JSON.

La forma del JSON clínico producido (`ClinicalJson`) se deja como mapa de claves de sección
a objetos: es salida de un LLM, así que cualquier sección o campo puede faltar y el tipado
estructural daría una garantía falsa. La validación campo a campo tiene su sitio en
`json_validator`, que llega con su propio PR; aquí se fija lo que sí es contrato estable.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

DocVariant = Literal["paper", "ficha_tecnica", "guia_clinica"]

SectionKey = str


class SectionSpec(TypedDict):
    """Descriptor de una sección del esquema clínico.

    `fields` es la lista de claves de la sección, y de ella depende el renderizado de los chunks.

    `description` es el prompt de extracción, así que **solo la llevan las variantes que se
    extraen con el LLM**: papers y guías. Las fichas técnicas se construyen desde CIMA, que las
    sirve ya segmentadas, y su mapa no describe una extracción sino la agrupación de campos para
    el troceado de indexado. Dejar ahí un prompt que nada lee sería declarar una capacidad que no
    existe.
    """

    name: str
    fields: list[str]
    description: NotRequired[str]
    verbatim: NotRequired[list[str]]
    """Campos de esta sección que el revisor de fidelidad ancla como **subcadena exacta**, no por
    términos raros: vocabulario controlado o nombres propios (fármacos, biomarcadores, escalas)
    donde parafrasear es un defecto. Se declara aquí, en el esquema, y no en el revisor, para que
    la lista salga de `SECTIONS_BY_VARIANT` y no diverja al primer renombrado —la misma regla que
    `field_provenance`—. Lo que no se declara se ancla por términos raros (paráfrasis), que es lo
    más indulgente y el defecto correcto para el riesgo de falso positivo del plan de fidelidad.
    """


SectionMap = dict[SectionKey, SectionSpec]

ClinicalJson = dict[SectionKey, Any]

Provenance = Literal["document", "external", "engine", "retired"]
"""De dónde sale el valor de un campo del JSON clínico.

`document`: lo extrajo el LLM del texto del documento, así que el revisor de fidelidad puede
anclarlo contra el Markdown fuente. `external`: viene de un registro oficial (BiFiMed, CIMA,
PubMed) y se verifica contra su fuente, no contra el documento. `engine`: lo escribe el código
del pipeline y no se revisa. Sin esta distinción el revisor marcaría como fabricado el bloque
de financiación entero y cada ficha construida desde CIMA.
"""

NON_DOCUMENT_PROVENANCE: dict[str, Provenance] = {
    "reimbursement": "external",
    "_source": "external",
    "national_codes": "external",
    "active_ingredient": "external",
    "strength": "external",
    "route_of_administration": "external",
    "_usage": "engine",
    "_extraction": "engine",
    "_content_check": "engine",
    "treatment_pathways.condition_ref": "engine",
    "recommendations.condition_ref": "engine",
    "special_populations.condition_refs": "engine",
    "therapies_index.pathway_refs": "engine",
    "schema_version": "engine",
    "metadata.extraction_date": "engine",
    "references": "retired",
    "toc": "retired",
    "glossary": "retired",
    "abbreviations": "retired",
    "funding": "retired",
    "conflicts_of_interest": "retired",
    "search_strategy": "retired",
    "databases": "retired",
    "keywords.enriched": "retired",
}
"""Los cuatro campos de CIMA están aquí porque **no salen del texto de la ficha**.

`national_codes` no lo declara ninguna sección, así que sin esta entrada `field_provenance` lanzaba
el `KeyError` que reserva para un desajuste de esquema — y la clave está en las cinco fichas
versionadas, así que la fase 3 del revisor moría en la primera que recorriera. Los otros tres los da
el metadato estructurado del registro (`vtm`, `principiosActivos`, `viasAdministracion`), no el
cuerpo del SmPC, así que anclarlos contra el documento los marcaría como fabricados: el falso
positivo garantizado que la precondición 1 del plan existe para evitar.

Ojo con los tres que **sí** salen de sección pero tienen respaldo en el metadato —titular, número de
autorización y forma farmacéutica—: siguen declarados como `document` porque es de donde vienen
normalmente, y solo caen al metadato en las fichas a las que CIMA no segmentó esa sección. La fase 3
tendrá que mirar el informe de fidelidad para distinguirlo; queda anotado en el plan.
"""
"""Los campos cuya procedencia no es el documento, con ruta punteada para los anidados.

Se aplican sobre cualquier variante: la clave puede no existir en un documento concreto
(`reimbursement` solo aparece en fichas), pero si aparece, esta es su procedencia.
"""


class SourceRef(TypedDict):
    """Bloque `_source` de una ficha técnica construida desde CIMA/AEMPS, sin LLM.

    Es el ancla de fidelidad de las fichas: `section_hashes` lleva un SHA por sección normativa
    del SmPC, así que la verificación va contra el registro oficial y no contra un Markdown que
    no existe. `smpc_date` es epoch en milisegundos, tal cual lo sirve CIMA. Hoy lo escribe el
    script de construcción (tarea 6 del TODO: absorberlo a `src/`); el contrato se declara aquí
    para que la ingesta que lo absorba no lo invente de nuevo.
    """

    origin: str
    registration_number: str
    name: str | None
    atc: list[str]
    national_codes: list[str]
    generic: bool
    smpc_date: int | None
    pdf_url: str | None
    active_ingredient_salt: str | None
    section_hashes: dict[str, str]