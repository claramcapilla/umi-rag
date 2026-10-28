"""Constantes del pipeline de conversión.

`CHUNK_MAX_CHARS` es el troceado de **generación** (cuánto texto entra en cada llamada al
LLM), no el del indexado, que vive en los settings de Django con valores mucho menores.
`LLM_RETRY_BASE_DELAY` está en segundos y se duplica en cada intento.

Los timeouts de PDF acotan subprocesos externos (la JVM de opendataloader, ghostscript).
Sin ellos un PDF que cuelgue el proceso bloquea el worker indefinidamente, y como la etapa
PDF→JSON pasa por una sola cola, un único documento wedgea la ingesta entera.
"""

from __future__ import annotations

import os
import re


def _int_env(name: str, default: int) -> int:
    """Entero desde el entorno, o el default. Los constreñimientos de hardware (inferencia local
    en GPU pequeño) se pasan por env desde el despliegue, no se cablean como default del motor."""
    return int(os.environ.get(name, str(default)))


LLM_MAX_TOKENS = _int_env("LLM_MAX_TOKENS", 16384)
"""Tope de salida por sección, holgado a propósito: lo comparte el razonamiento.

Con los modelos `deepseek-v4-*` los tokens de pensamiento salen de este mismo presupuesto, así
que con 8192 la sección `artifacts` de una guía —la más grande, con tablas— volvía con **0
caracteres** y sin error, y otra sección llegaba truncada. `LLM_REASONING` acota el razonamiento
pero, salvo en `disabled`, no lo elimina: medido, `low` lo reduce a la mitad, no a cero. Por eso
el default es 16384 y es el endpoint en la nube quien manda.

En inferencia local el tope se baja por env, y por dos motivos que se sostienen a la vez: el
modelo coder corre con `LLM_REASONING=disabled`, así que la salida es solo contenido (máximo
medido ~3.4k tokens en `treatment_pathways`), y el presupuesto está **acoplado** al de entrada
—`entrada + salida <= num_ctx`—, de modo que reservar 16384 para una salida que no llega a 4k
gasta ventana que el extracto necesita. Rebajarlo libera esos tokens para `GEN_CHUNK_MAX_CHARS`.
"""
LLM_TEMPERATURE = 0.0
"""Cero: extraer no es muestrear.

El mismo argumento que ya fijó `ROUTER_TEMPERATURE` a 0 aplica a las secciones —un flip entre
reprocesos cambia el contenido extraído sin que nada haya cambiado en el documento— y el plan de
fidelidad lo convierte en requisito: la línea base se mide contra un pipeline que no sortea.
"""
LLM_SEED = 42
"""Mejor esfuerzo, y conviene no prometer más.

Se pasa en cada llamada sin razonamiento (con `low`/`high` la API no lo admite), pero no da
reproducibilidad: manda el batching del
proveedor (MoE, el enrutado a expertos depende del lote del servidor), el `seed` de las APIs
compatibles con OpenAI no es vinculante, y la traza de razonamiento se muestrea igual. El suelo
de ruido se mide con N ejecuciones, no se elimina con esta constante.
"""
# Default dimensionado para el endpoint en la nube (ventana de contexto amplia). La inferencia
# local en un GPU pequeño va lenta (~20 tok/s) y una sección grande no termina en 120 s, así que
# el despliegue local sube el plazo por env (`LLM_TIMEOUT_SECONDS`, p. ej. 1200): no está colgada,
# va lenta. No se cablea aquí para no penalizar al endpoint rápido con un timeout de 20 minutos.
LLM_TIMEOUT_SECONDS = _int_env("LLM_TIMEOUT_SECONDS", 120)
LLM_MAX_RETRIES = max(1, _int_env("LLM_MAX_RETRIES", 3))
LLM_RETRY_BASE_DELAY = 2

# Troceado de GENERACIÓN (cuánto texto entra en cada llamada al LLM). El default asume la ventana
# amplia del endpoint en la nube. En inferencia local, el extracto más la salida (`LLM_MAX_TOKENS`)
# tienen que caber en `num_ctx`, o el servidor recorta el prompt por delante y se lleva el system
# prompt (la contención); el despliegue local lo baja por env (`GEN_CHUNK_MAX_CHARS`, p. ej. 40000,
# ≈10-13k tokens de entrada). Es el tamaño de cada ventana: un extracto más largo se parte en
# ventanas consecutivas, cada una en su llamada, y nada se recorta. Nombre propio, para no
# colisionar con `CHUNK_MAX_CHARS` de settings (el indexado).
CHUNK_MAX_CHARS = _int_env("GEN_CHUNK_MAX_CHARS", 200_000)
CHUNK_HEAD_CONTEXT_CHARS = _int_env("GEN_CHUNK_HEAD_CONTEXT_CHARS", 12_000)
CHUNK_TAIL_CONTEXT_CHARS = _int_env("GEN_CHUNK_TAIL_CONTEXT_CHARS", 8_000)

PDF_CONVERT_TIMEOUT_SECONDS = 300
PDF_CHUNKING_CONVERT_TIMEOUT_SECONDS = 1800
PDF_REPAIR_TIMEOUT_SECONDS = 120

DETECTOR_SMPC_THRESHOLD = 3
DETECTOR_GUIDELINE_THRESHOLD = 3

ROUTER_CLASSIFY_CHARS = 4_000
ROUTER_MAX_TOKENS = 2_048
ROUTER_TEMPERATURE = 0.0
"""Enrutado de variante paper/guía: se decide con el LLM, no con `DETECTOR_GUIDELINE_THRESHOLD`.

Medido sobre 162 documentos de alergología con su JSON clínico como referencia: el detector de
regex acierta el 15% de las guías (11 de 75), apenas por encima de un clasificador constante que
dijera siempre «paper» (54% frente al 59% del detector). La causa es que `_GUIDELINE_PATTERNS` se
escribió para un corpus de hemato-oncología (NCCN, ESMO, GELTAMO, estadificación, índice
pronóstico) y no incluye ni la palabra «guideline» a secas, así que en otra especialidad puntúa
cero. Las distribuciones de `effective_cg` de guías y papers se solapan por completo, de modo que
no hay umbral que las separe: no es un umbral mal calibrado, es una señal ausente.

`DETECTOR_SMPC_THRESHOLD` sí se queda: la ficha técnica está limpiamente separada (159 de esos
162 documentos puntúan 0 y el máximo es 2), así que esa rama no necesita gastar una llamada.

`ROUTER_TEMPERATURE` es una constante propia y no un alias de `LLM_TEMPERATURE` aunque hoy las
dos valgan 0: esta decisión elige el esquema completo del documento, y un flip entre reprocesos
cambiaría su forma y la procedencia de sus chunks, así que subir la temperatura de sección para
un experimento no debe arrastrar la del enrutado.

`ROUTER_MAX_TOKENS` es holgado para lo que se pide —la respuesta son dos claves, unos 20
tokens— porque el tope no lo consume solo el contenido: un modelo de razonamiento gasta ahí sus
tokens de pensamiento y devuelve contenido vacío, y eso no falla un documento sino **todos**.
No es hipotético: con 256 y `LLM_REASONING=low`, `deepseek-v4-pro` tumbó 2 de los 4 primeros
documentos de una tanda con «JSON irrecuperable en sección clasificador_variante (0
caracteres)». Medido: razonar una tarea difícil le cuesta ~471 tokens con `low`, así que 2.048
deja margen de sobra sin encarecer nada, porque solo se paga lo que se emite.
"""

FOLDER_MAX_WORKERS = 4
FILE_MAX_WORKERS = 3

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)