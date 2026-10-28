from __future__ import annotations

from document_processor.schema import SectionSpec

CLAVE_DOCUMENTO_POR_APARTADOS = "document_by_section"
CAMPO_DE_APARTADOS = "document_sections"

SECCION_DOCUMENTO_POR_APARTADOS: SectionSpec = {
    "name": "Documento por apartados",
    "fields": [CAMPO_DE_APARTADOS],
    "description": """Recorre el documento entero en su orden, \
apartado por apartado, y reescribe en tus palabras todo lo que afirma.

**Campos requeridos:**
```json
{
  "document_sections": [
    {
      "heading": "encabezado del apartado tal como aparece en el documento",
      "content": "todas las afirmaciones del apartado, reescritas"
    }
  ]
}
```

**Reglas:**
- Un elemento por cada apartado del documento con contenido clínico o
  científico, en el orden del documento, incluidos resumen, métodos,
  resultados, discusión, limitaciones y conclusiones.
- "content" recoge TODAS las afirmaciones del apartado: hallazgos,
  definiciones, criterios, recomendaciones con su fuerza, advertencias,
  limitaciones, comparaciones con otros estudios (atribuidas a esos estudios)
  y conclusiones. Ninguna afirmación se omite.
- Cifras, dosis, unidades, calificadores, cuantificadores y cautelas
  exactamente como en el documento.
- Toda cifra del texto va en el "content" de su apartado, también las de
  análisis de sensibilidad, estudios citados y pacientes excluidos.
- Reescrito en frases breves y propias: nunca copies frases del documento.
- Se omiten agradecimientos, financiación, conflictos de interés, autoría,
  bibliografía, listas de abreviaturas y texto editorial sin contenido clínico.
- Las tablas no se repiten aquí: basta con decir qué muestran.""",
}