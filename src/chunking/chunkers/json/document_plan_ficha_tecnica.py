"""Plan jerárquico para dividir fichas técnicas de medicamentos.

Agrupa la información por secciones clínicas y administrativas. Las hojas del
árbol señalan las rutas JSON que se incorporan a cada grupo de chunks.
"""

from chunking.chunkers.json.JSON_chunker import SplitNode

DOCUMENT_PLAN_FC = SplitNode(
    name="complete_document",
    children=[
        # Identidad del medicamento y composición declarada.
        SplitNode(
            name="identification",
            children=[
                SplitNode(
                    name="medicine_name_pharmaceutical_form",
                    children=[
                        SplitNode(
                            name="medicine_name",
                            fields=["medicine_name"]
                        ),
                        SplitNode(
                            name="pharmaceutical_form",
                            fields=["pharmaceutical_form"]
                        )
                    ]
                ),
                SplitNode(
                    name="composition",
                    children=[
                        SplitNode(
                            name="active_ingredient_amount",
                            fields=["composition.active_ingredient_amount"]
                        ),
                        SplitNode(
                            name="excipients_with_known_effect",
                            fields=["composition.excipients_with_known_effect"]
                        )
                    ]
                )
            ]
        ),

        # Uso clínico: indicaciones, pauta y situaciones en que no debe usarse.
        SplitNode(
            name="indications",
            fields=["indications"]
        ),

        SplitNode(
            name="posology",
            fields=["posology"]
        ),

        SplitNode(
            name="contraindications",
            fields=["contraindications"]
        ),

        # Precauciones y efectos relevantes para el tratamiento y la seguridad.
        SplitNode(
            name="warnings_and_precautions",
            fields=["warnings_and_precautions"]
        ),

        SplitNode(
            name="interactions",
            fields=["interactions"]
        ),

        SplitNode(
            name="pregnancy_and_lactation",
            fields=["pregnancy_and_lactation"]
        ),

        SplitNode(
            name="effects_on_driving",
            fields=["effects_on_driving"]
        ),

        SplitNode(
            name="adverse_reactions",
            fields=["adverse_reactions"]
        ),

        # Información farmacológica del medicamento.
        SplitNode(
            name="overdose",
            fields=["overdose"]
        ),

        SplitNode(
            name="pharmacodynamics",
            fields=["pharmacodynamics"]
        ),

        SplitNode(
            name="pharmacokinetics",
            fields=["pharmacokinetics"]
        ),

        # Propiedades farmacéuticas, conservación y manipulación.
        SplitNode(
            name="pharmaceutical",
            children=[
                SplitNode(
                    name="excipients",
                    fields=["excipients"]
                ),
                SplitNode(
                    name="incompatibilities_shelf_life",
                    children=[
                        SplitNode(
                            name="incompatibilities",
                            fields=["incompatibilities"]
                        ),
                        SplitNode(
                            name="shelf_life",
                            fields=["shelf_life"]
                        )
                    ]
                ),
                SplitNode(
                    name="storage_container_handling",
                    children=[
                        SplitNode(
                            name="storage_container",
                            children=[
                                SplitNode(
                                    name="storage_conditions",
                                    fields=["storage_conditions"]
                                ),
                                SplitNode(
                                    name="container",
                                    fields=["container"]
                                )
                            ]
                        ),
                        SplitNode(
                            name="handling_instructions",
                            fields=["handling_instructions"]
                        )
                    ]
                )
            ]
        ),

        # Titular y número de autorización de comercialización.
        SplitNode(
            name="marketing",
            children=[
                SplitNode(
                    name="marketing_authorisation_holder",
                    fields=["marketing_authorisation_holder"]
                ),
                SplitNode(
                    name="marketing_authorisation_number",
                    fields=["marketing_authorisation_number"]
                )
            ]
        ),

        # Mantiene juntos principio activo, concentración y vía de administración
        # para que la información esencial de administración conserve su contexto.
        SplitNode(
            name="administration",
            fields=[
                "active_ingredient",
                "strength",
                "route_of_administration"
            ]
        ),

        # Datos de financiación, presentaciones y códigos nacionales consultados.
        SplitNode(
            name="reimbursement",
            children=[
                SplitNode(
                    name="info_reimboursement",
                          children=[
                              SplitNode(
                                  name="data_presentations",
                                  children=[
                                      SplitNode(
                                          name="status_queried_by",
                                          children=[
                                              SplitNode(
                                                  name="status",
                                                  fields=["status"]
                                              ),
                                              SplitNode(
                                                  name="queried_by",
                                                  fields=["queried_by"]
                                              )
                                          ]
                                      ),
                                      SplitNode(
                                          name="source_n",
                                          children=[
                                              SplitNode(
                                                  name="source",
                                                  fields=["source"]
                                              ),
                                              SplitNode(
                                                  name="n_presentations",
                                                  fields=["n_presentations"]
                                              )
                                          ]
                                      )
                                  ]
                              ),
                              SplitNode(
                                  name="presentations",
                                  fields=["presentations"]
                              )
                          ]
                          ),
                SplitNode(
                    name="national_codes_queried",
                    fields=["national_codes_queried"]
                )
            ]
        )
    ]
)