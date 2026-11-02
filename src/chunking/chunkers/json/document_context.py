"""Campos de contexto documental incluidos en los metadatos de cada chunk."""

CONTEXT_FIELDS = {
    "paper": ["doi", "title", "journal", "year"],
    "guideline": [
        "metadata.title",
        "metadata.organization",
        "metadata.version",
    ],
    "ficha_tecnica": [
        "medicine_name",
        "pharmaceutical_form",
        "active_ingredient",
        "strength",
    ],
}
