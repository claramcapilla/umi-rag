# umi-rag

Repositorio en desarrollo para un sistema RAG de documentos clínicos. El código
Python usa una estructura `src/` directa: los paquetes (`indexing`,
`retrieval`, `generation`, `chatbot`, `chunking` y `document_processor`)
están dentro de `src/`, junto con `rag` e `ingestion`.

## Estado actual

- **Indexación:** normaliza chunks JSONL o resultados `chunk_results`, calcula
  embeddings con caché y hace upsert en Qdrant.
- **Retrieval:** busca chunks para una pregunta usando el modelo de embeddings
  configurado. No genera respuestas.
- **Procesamiento documental:** hay módulos de conversión PDF y limpieza. El
  chunking se realiza con una estrategia jerárquica para JSON clínico.
- **RAG:** `src/rag/service.py` conecta la recuperación con la generación.
  La aplicación Django usa este servicio desde el chat; la consulta requiere
  Qdrant y un servidor de generación compatible con la API de OpenAI.
- **Web:** `web/` contiene la aplicación Django, el chat y el flujo de gestión
  documental. Las tareas de procesamiento e indexación se coordinan mediante
  Celery y Redis.

## Requisitos e instalación

El proyecto declara Python `>=3.12` en `pyproject.toml`. Python 3.14 es una
versión válida para la instalación, aunque el proyecto todavía debe validarse
con sus pruebas en esa versión.

Desde la raíz del repositorio, en PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[indexing,chunking]"
```

La instalación editable registra los paquetes que están en `src/` para que
puedan importarse directamente como `indexing`, `retrieval`, etc. El extra
`indexing` instala las dependencias de embeddings y Qdrant (incluidos `numpy`
y `qdrant-client`); `chunking` instala las dependencias de la estrategia JSON.
Ambos extras se incluyen para ejecutar el pipeline y los tests. PyTorch puede
requerir una instalación específica según el hardware y la versión de CUDA.

`pyproject.toml` es el archivo estándar de configuración y metadatos de un
proyecto Python: aquí declara el nombre, la versión, las dependencias, la
versión de Python y cómo descubrir los paquetes de `src/`.

## Chunking JSON

El chunker jerárquico procesa los JSON de `data/processed/json` y escribe los
resultados bajo `data/processed/chunks/json/`, una ubicación que ya recorre la
normalización de indexación. Las rutas y los parámetros del tokenizador se
configuran en `configs/chunker_config.json`; las rutas relativas se resuelven
desde la raíz del repositorio.

```powershell
python scripts\run_chunking.py
```

Para ajustar el paralelismo o desactivar la caché:

```powershell
python scripts\run_chunking.py --workers 2 --no-cache
```

## Indexación

La indexación acepta JSONL canónico (un chunk por línea) y archivos JSON con
resultados `chunk_results`. Para indexar:

1. Configura `embeddings.model_name` y la conexión/colección de Qdrant en
   `configs/indexing_config.json`.
2. Coloca los chunks en `data/processed/chunks/` o cambia `paths.chunks_dir`.
3. Arranca Qdrant.

Ejecuta desde la raíz:

```powershell
python scripts\run_indexing.py
```

Para validar y escribir los chunks normalizados sin cargar el modelo ni
conectar con Qdrant:

```powershell
python scripts\run_indexing.py --normalize-only
```

La indexación es incremental y repetible: los IDs deterministas y la caché
evitan duplicar puntos o recalcular embeddings que no han cambiado. El pipeline
no elimina puntos antiguos de Qdrant cuando desaparece un documento o cambian
sus IDs.

## Recuperación y generación

El retrieval toma su límite de resultados de `configs/retrieval_config.json` y
busca los chunks relevantes en Qdrant. También se puede usar desde Python:

```python
from retrieval.retriever import Retriever

with Retriever() as retriever:
    hits = retriever.retrieve("¿Cuál es la recomendación?", top_k=5)
```

El servicio de `rag` combina esa recuperación con la generación de respuestas.
La aplicación web lo utiliza en el chat.

## Aplicación web

Instala las dependencias de la aplicación y sus extras desde la raíz:

```powershell
python -m pip install -r web\requirements.txt
```

Configura `DJANGO_SECRET_KEY` (obligatoria) y, si no se usan los valores por
defecto, `VLLM_URL` y `VLLM_MODEL`. Después aplica las migraciones, crea un
usuario para iniciar sesión y arranca Django:

```powershell
$env:DJANGO_SECRET_KEY = "cambia-esta-clave"
python web\manage.py migrate
python web\manage.py createsuperuser
python web\manage.py runserver
```

El chat requiere Qdrant y un servidor de generación compatible con la API de
OpenAI. El procesamiento documental asíncrono requiere además Redis y un worker
de Celery.

## Pruebas

Con el entorno virtual y el proyecto instalados:

```powershell
python -m unittest discover -s tests -v
```

## Estructura principal

```text
configs/       Configuración de chunking, indexación y retrieval
data/          Datos de ejemplo y datos de trabajo
scripts/       Lanzadores de tareas
src/
  chatbot/     Componentes de conversación y citas
  chunking/    Chunking jerárquico JSON y caché de resultados
  document_processor/
  generation/  Prompts y cliente/generador LLM
  indexing/    Normalización, embeddings, caché y Qdrant
  ingestion/   Adaptación de documentos clínicos al chunker e indexador
  rag/         Coordinación de recuperación y generación
  retrieval/   Búsqueda y construcción de contexto
tests/         Pruebas unitarias
web/           Aplicación Django: chat, documentos y tareas de pipeline
```
