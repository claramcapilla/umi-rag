"""Redacción de datos personales del Markdown, preservando la procedencia bibliográfica.

Sustituye al `--sanitize` de opendataloader-pdf, que hacía tres cosas mal, medidas sobre los
162 documentos del corpus de alergología:

1. **Destruía toda URL**: 10.452 reemplazos por `https://example.com` en 155 documentos,
   incluidos los enlaces a la EMA y a la FDA de las referencias. Al quitarlo, los DOI
   distintos del corpus pasan de 1.925 a 3.955.
2. **Falsificaba identificadores bibliográficos**: 692 apariciones de `AA0000000` en 78
   documentos, que eran números de artículo de citas Cochrane, becas del NIH
   (`K23AI175525` → `K23AA0000000`), ORCID, numeración de secciones y alelos HLA.
3. **Sustituía por valores plausibles en vez de marcarlos**, así que aguas abajo nadie podía
   saber que el dato era inventado. Es la familia de la afirmación falsa de BiFiMed.

**Solo se redactan correos**, y la razón es empírica, no de pereza. La primera versión de este
módulo también buscaba teléfonos, tarjetas e IP, y medida sobre el corpus resultó ser el mismo
error que le criticábamos al JAR:

- El patrón telefónico daba 164 aciertos, **todos falsos positivos**, incluidas las secuencias
  de dosis `100-150-200` y `125-250-500`, que desaparecieron del Markdown.
- El de tarjetas daba 106, y eran **ORCID** (`https://orcid.org/0000-0002-…`) y sufijos de DOI:
  una cadena de ceros pasa Luhn.
- El de IP daba 56, y eran números de versión de software y referencias cruzadas.

La tasa base de teléfonos de paciente y de tarjetas en literatura médica publicada es cero, y
el coste de equivocarse es borrar una posología. De ahí la regla: **lo que no se puede validar
por sintaxis, no se toca**. El correo sí es inequívoco, y hay 225 en 133 documentos.

**Esto no es anonimización, es minimización.** El nombre y la afiliación del autor de
correspondencia siguen ahí, y deben seguir: son información bibliográfica que la regla de
dominio obliga a conservar. Lo que se quita es una superficie de contacto que el RAG nunca
necesita y que hoy salía hacia los dos egress —el LLM recibe el documento entero y el de
embeddings cada chunk—.

El ámbito de datos que sostiene todo esto —**solo literatura médica publicada**— está
declarado en `CLAUDE.md`. Si algún día entran informes o historias con datos de paciente, esto
**no basta**: haría falta el detector de identificadores, que es precondición de esa
ampliación y no un deseo.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MARCADOR = "[IDENTIFICADOR OMITIDO]"
"""El literal con el que se redactan los correos.

Un solo token en todo el corpus hace que buscarlo sea una comprobación de auditoría exacta de
dónde había un dato personal. Con dos, quien audite dentro de un año encuentra la mitad. Y un
marcador que dijera «CORREO» le contaría además al LLM qué había ahí, que es justo lo que no
interesa.
"""

_EMAIL_RE = re.compile(r"\b[\w.%+-]{1,64}@ ?[\w.-]{1,255}\.[A-Za-z]{2,24}\b")
"""Cuantificadores acotados a los límites del RFC 5321, y no por purismo.

Sin cota, el sufijo retrocede una vez por carácter en cada intento fallido y el coste es
cuadrático sobre una entrada patológica. Importa aquí más que en otros módulos porque esta
función corre **antes** que `clean_markdown`, y por tanto antes de
`_collapse_pathological_runs`, que es la guarda que se añadió al encontrar tres ReDoS reales
con 2.000 caracteres. Es el único paso de regex sobre markdown crudo sin esa protección, con
una sola cola de ingesta detrás.
"""


def redact_personal_data(texto: str) -> str:
    """Redacta correos del Markdown dejando intacta la procedencia bibliográfica.

    No toca URL, DOI, ORCID, PMID, identificadores de ensayo (NCT), números de beca ni de cita:
    son lo que permite citar la fuente, que es regla innegociable del dominio.

    Registra **cuántos** redactó, nunca cuáles: da constancia de que la medida se aplicó, y
    sirve de canario, porque un documento con cuarenta correos no es un paper.
    """
    redactado, n = _EMAIL_RE.subn(MARCADOR, texto)
    if n:
        logger.info("Redacción: %d correos omitidos", n)
    return redactado