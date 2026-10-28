"""Secciones del esquema de fichas técnicas (SmPC), agrupadas para el troceado de indexado.

Este mapa **ya no describe una extracción**: las fichas se construyen desde CIMA, que las sirve
segmentadas por su numeración normativa, así que aquí no queda ningún prompt. Lo que queda es lo
que consume `chunking/rag_index_chunker.py` —qué campos van juntos en un chunk y con qué nombre
legible— más la lista de los 25 campos, que es la fuente del mapeo sección normativa → campo en
`cima/builder.py`.

Con el camino PDF+LLM se fueron también los `heading_prefixes`: servían para localizar la sección
en un Markdown convertido, y ese Markdown ya no existe en este camino.
"""

from __future__ import annotations

from src.document_processor.schema import SectionMap

SECTIONS_FT: SectionMap = {
    "identification": {
        "name": "Identificación del Medicamento",
        "fields": [
            "medicine_name",
            "active_ingredient",
            "strength",
            "pharmaceutical_form",
            "route_of_administration",
            "composition",
            "marketing_authorisation_holder",
            "marketing_authorisation_number",
        ],
    },
    "clinical_indications": {
        "name": "Indicaciones Terapéuticas",
        "fields": ["indications"],
    },
    "clinical_posology": {
        "name": "Posología y Forma de Administración",
        "fields": ["posology"],
    },
    "clinical_contraindications": {
        "name": "Contraindicaciones",
        "fields": ["contraindications"],
    },
    "clinical_warnings": {
        "name": "Advertencias y Precauciones",
        "fields": ["warnings_and_precautions"],
    },
    "clinical_interactions": {
        "name": "Interacciones Medicamentosas",
        "fields": ["interactions"],
    },
    "clinical_pregnancy": {
        "name": "Embarazo, Lactancia y Conducción",
        "fields": ["pregnancy_and_lactation", "effects_on_driving"],
    },
    "clinical_adverse": {
        "name": "Reacciones Adversas",
        "fields": ["adverse_reactions"],
    },
    "clinical_overdose": {
        "name": "Sobredosis",
        "fields": ["overdose"],
    },
    "pharmacology": {
        "name": "Propiedades Farmacológicas",
        "fields": ["pharmacodynamics", "pharmacokinetics"],
    },
    "pharmaceutical": {
        "name": "Datos Farmacéuticos",
        "fields": [
            "excipients",
            "incompatibilities",
            "shelf_life",
            "storage_conditions",
            "container",
            "handling_instructions",
        ],
    },
}