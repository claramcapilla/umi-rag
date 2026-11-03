"""Plan jerárquico para dividir guías y recomendaciones clínicas.

Organiza metadatos, alcance, contenido clínico y material complementario.
La jerarquía agrupa campos relacionados y separa secciones conceptualmente
distintas.
"""

from chunking.chunkers.json.JSON_chunker import SplitNode

DOCUMENT_PLAN_GL = SplitNode(
    name="complete_document",
    children=[
    # Identificación, procedencia, versión y público destinatario de la guía.
    SplitNode(
        name="metadata",
        children=[
            # Identificación formal y versionado del documento.
            SplitNode(
                name="document_identification",
                children=[
                    SplitNode(
                        name="version_type",
                        children=[
                            SplitNode(
                                name="schema_version",
                                fields=["schema_version"]
                            ),
                            SplitNode(
                                name="document_type",
                                fields=["document_type"]
                            )
                        ]
                    ),
                    # El título, la organización y los autores se agrupan para
                    # identificar conjuntamente quién publica la guía.
                    SplitNode(
                        name="title_authors_org",
                        children=[
                            SplitNode(
                                name="title_org",
                                children=[
                                    SplitNode(
                                        name="title",
                                        fields=["metadata.title"]
                                    ),
                                    SplitNode(
                                        name="organization",
                                        fields=["metadata.organization"]
                                    )
                                ]
                            ),
                            SplitNode(
                                name="authors",
                                fields=["metadata.authors"]
                            )
                        ]
                    )
                ]
            ),
            # Datos de publicación, versión y extracción de la fuente.
            SplitNode(
                name="source_version",
                children=[
                    SplitNode(
                        name="year_version_language",
                        children=[
                            # Año y versión sitúan temporalmente la guía;
                            # el idioma completa la identificación de la edición.
                            SplitNode(
                                name="year_version",
                                children=[
                                    SplitNode(
                                        name="year",
                                        fields=["metadata.year"]
                                    ),
                                    SplitNode(
                                        name="version",
                                        fields=["metadata.version"]
                                    )
                                ]
                            ),
                            SplitNode(
                                name="language",
                                fields=["metadata.language"]
                            )
                        ]
                    ),
                    SplitNode(
                        name="pages_extraction",
                        children=[
                            SplitNode(
                                name="source_pages",
                                fields=["metadata.source_pages"]
                            ),
                            SplitNode(
                                name="extraction_date",
                                fields=["metadata.extraction_date"]
                            )
                        ]
                    )
                ]
            ),
            # Audiencia y especialidad describen a quién se dirige; la
            # jurisdicción identifica el ámbito geográfico de aplicación.
            SplitNode(
                name="audience_specialty_jurisdiction",
                children=[
                    SplitNode(
                        name="audience_specialty",
                        children=[
                            SplitNode(
                                name="audience",
                                fields=["metadata.audience"]
                            ),
                            SplitNode(
                                name="specialty",
                                fields=["metadata.specialty"]
                            )
                        ]
                    ),
                    SplitNode(
                        name="jurisdiction",
                        fields=["metadata.jurisdiction"]
                    )
                ]
            ),
        ]
    ),
    # Se mantiene separado de la síntesis: son indicadores de calidad, no
    # contenido narrativo de la guía.
    SplitNode(
        name="quality_flags",
        fields=["metadata.quality_flags"]
    ),
    # Síntesis general del contenido y las recomendaciones de la guía.
    SplitNode(
        name="summary",
        fields=["metadata.summary"]
    ),
    # Alcance clínico: condiciones, áreas terapéuticas, población y entornos.
    SplitNode(
        name="scope",
        children=[
            # Relaciona las condiciones abordadas con sus áreas terapéuticas.
            SplitNode(
                name="conditions_therapeutic_areas",
                children=[
                    SplitNode(
                        name="conditions_covered",
                        fields=["scope.conditions_covered"]
                    ),
                    SplitNode(
                        name="therapeutic_areas",
                        fields=["scope.therapeutic_areas"]
                    )
                ]
            ),
            # Agrupa la población destinataria con los entornos donde se atiende.
            SplitNode(
                name="target_population_care_settings",
                children=[
                    SplitNode(
                        name="target_population",
                        fields=["scope.target_population"]
                    ),
                    SplitNode(
                        name="care_settings",
                        fields=["scope.care_settings"]
                    )
                ]
            )
        ]
    ),
    # Índice jerárquico del documento.
    SplitNode(
        name="toc",
        fields=["toc"] # Se conserva cada elemento del índice completo.
    ),
    # Contenido clínico y recursos organizados por tema.
    SplitNode(
        name="conditions",
        fields=["conditions"] # Se conserva cada condición como una unidad completa.
    ),
    SplitNode(
        name="treatment_pathways",
        fields=["treatment_pathways"] # Cada itinerario se mantiene completo.
    ),
    SplitNode(
        name="therapies_index",
        fields=["therapies_index"] # Cada entrada del índice se mantiene completa.
    ),
    # Medidas de seguridad y manejo de toxicidades e infecciones.
    SplitNode(
        name="safety",
        children=[
            # Agrupa los distintos tipos de medidas de seguridad de la guía.
            SplitNode(
                name="toxicities",
                fields=["safety.toxicities"]
            ),
            SplitNode(
                name="prophylaxis",
                fields=["safety.prophylaxis"]
            ),
            SplitNode(
                name="infection_management",
                fields=["safety.infection_management"]
            )
        ]
    ),
    # Recomendaciones clínicas y materiales de apoyo relacionados.
    SplitNode(
        name="recommendations",
        fields=["recommendations"] # Cada recomendación se conserva como una unidad.
    ),
    SplitNode(
        name="artifacts",
        fields=["artifacts"] # Se mantienen completos los recursos asociados.
    ),
    SplitNode(
        name="special_populations",
        fields=["special_populations"] # Cada población especial se conserva completa.
    ),
    SplitNode(
        name="glossary",
        fields=["glossary"] # Cada término mantiene juntos su definición y contexto.
    ),
    SplitNode(
        name="abbreviations",
        fields=["abbreviations"] # Cada abreviatura se conserva con su significado.
    ),
    SplitNode(
        name="references",
        fields=["references"] # Cada referencia bibliográfica se mantiene completa.
    ), 
    SplitNode(
        name="keywords",
        fields=["keywords"] # Términos que describen los temas de la guía.
    ),
    SplitNode(
        name="usage",
        # Metadatos de uso auxiliares, fuera del contenido clínico.
        fields=["_usage"]
    )
]
)