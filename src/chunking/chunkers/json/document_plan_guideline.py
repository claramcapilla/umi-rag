from chunking.chunkers.json.JSON_chunker import SplitNode

DOCUMENT_PLAN_GL = SplitNode(
    name="complete_document",
    children=[
        # NIVEL 1
        SplitNode(
        name="metadata",
        children=[
            # NIVEL 2
            SplitNode(
                name="document_title_organization",
                children=[
                    # NIVEL 3
                    SplitNode(
                        name="version_type_title_org",
                        children=[
                            # NIVEL 4
                            SplitNode(
                                name="version_type",
                                children=[
                                    # NIVEL 5
                                    SplitNode(
                                        name="schema_version",
                                        fields=["schema_version"]
                                    ),
                                    # NIVEL 5
                                    SplitNode(
                                        name="document_type",
                                        fields=["document_type"]
                                    )
                                ]
                            ),
                            # NIVEL 4
                            SplitNode(
                                name="title_org",
                                children=[
                                    # NIVEL 5
                                    SplitNode(
                                        name="title",
                                        fields=["metadata.title"]
                                    ),
                                    # NIVEL 5
                                    SplitNode(
                                        name="organization",
                                        fields=["metadata.organization"]
                                    )
                                ]
                            )
                        ]
                    ),
                    # NIVEL 3
                    SplitNode(
                        name="authors",
                        fields=["metadata.authors"]
                    )
                ]
            ),
            # NIVEL 2
            SplitNode(
                name="source_version",
                children=[  
                    # NIVEL 3      
                    SplitNode(
                        name="year_version_language",
                        children=[
                            # NIVEL 4
                            SplitNode(
                                name="year_version",
                                children=[
                                    # NIVEL 5
                                    SplitNode(
                                        name="year",
                                        fields=["metadata.year"]
                                    ),
                                    # NIVEL 5
                                    SplitNode(
                                        name="version",
                                        fields=["metadata.version"]
                                    )
                                ]
                            ),
                            # NIVEL 4
                            SplitNode(
                                name="language",
                                fields=["metadata.language"]
                            )
                        ]
                    ),
                    # NIVEL 3
                    SplitNode(
                        name="pages_extraction",
                        children=[
                            # NIVEL 4
                            SplitNode(
                                name="source_pages",
                                fields=["metadata.source_pages"]
                            ),
                            # NIVEL 4
                            SplitNode(
                                name="extraction_date",
                                fields=["metadata.extraction_date"]
                            )
                        ]
                    )
                ]
            ),
            # NIVEL 2
            SplitNode(
                name="audience_specialty_jurisdiction",
                children=[
                    # NIVEL 3
                    SplitNode(
                        name="audience_specialty",
                        children=[
                            # NIVEL 4
                            SplitNode(
                                name="audience",
                                fields=["metadata.audience"]
                            ),
                            # NIVEL 4
                            SplitNode(
                                name="specialty",
                                fields=["metadata.specialty"]
                            )
                        ]
                    ),
                    # NIVEL 3
                    SplitNode(
                        name="jurisdiction",
                        fields=["metadata.jurisdiction"]
                    )
                ]
            ),
            # NIVEL 2
            SplitNode(
                name="flags_summary",
                children=[
                    # NIVEL 3
                    SplitNode(
                        name="quality_flags",
                        fields=["metadata.quality_flags"]
                    ),
                    # NIVEL 3
                    SplitNode(
                        name="summary",
                        fields=["metadata.summary"]
                    )
                ]
            )
        ]
    ),
    # NIVEL 1
    SplitNode(
        name="scope",
        children=[
            # NIVEL 2
            SplitNode(
                name="cond_care_population",
                children=[
                    # NIVEL 3
                    SplitNode(
                        name="cond_care",
                        children=[
                            # NIVEL 4
                            SplitNode(
                                name="conditions_covered",
                                fields=["scope.conditions_covered"]
                            ),
                            # NIVEL 4
                            SplitNode(
                                name="care_settings",
                                fields=["scope.care_settings"]
                            )
                        ]
                    ),
                    # NIVEL 3
                    SplitNode(
                        name="target_population",
                        fields=["scope.target_population"]
                    )
                ]
            ),
            # NIVEL 2
            SplitNode(
                name="therapeutic_areas",
                fields=["scope.therapeutic_areas"]
            )
        ]
    ),
    SplitNode(
        name="toc",
        fields=["toc"] # lista de diccionarios indivisibles
    ),
    # NIVEL 1
    SplitNode(
        name="conditions",
        fields=["conditions"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="treatment_pathways",
        fields=["treatment_pathways"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="therapies_index",
        fields=["therapies_index"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="safety",
        children=[
            # NIVEL 2
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
    # NIVEL 1
    SplitNode(
        name="recommendations",
        fields=["recommendations"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="artifacts",
        fields=["artifacts"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="special_populations",
        fields=["special_populations"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="glossary",
        fields=["glossary"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="abbreviations",
        fields=["abbreviations"] # lista de diccionarios indivisibles
    ),
    SplitNode(
        name="references",
        fields=["references"] # lista de diccionarios indivisibles
    ), 
    SplitNode(
        name="keywords",
        fields=["keywords"] # lista de diccionarios indivisiblesildren=[
    ),
    SplitNode(
        name="usage",
        fields=["_usage"]
    )
]
)