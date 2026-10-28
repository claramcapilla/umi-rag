# umi-rag

Repositorio en desarrollo para un sistema RAG de documentos clínicos. El código
Python usa una estructura `src/` directa: los paquetes (`indexing`,
`retrieval`, `generation`, `chatbot`, `chunking` y `document_processor`)
están dentro de `src/`.

## Estado actual

- **Indexación:** normaliza chunks JSONL o resultados `chunk_results`, calcula
  embeddings con caché y hace upsert en Qdrant.
- **Retrieval:** busca chunks para una pregunta usando el modelo de embeddings
  configurado. No genera respuestas.
- **Procesamiento documental:** hay módulos de conversión PDF y limpieza, y
  se ha integrado la estrategia jerárquica de chunking JSON. Las estrategias
  del benchmark para Markdown aún no se han migrado.
- **Generación y chatbot:** existen componentes en `src/generation/` y
  `src/chatbot/`; todavía no están conectados a la aplicación web.
- **Web:** `web/` contiene un prototipo Django separado. Su vista aún importa
  `rag.service`, que no está en este repositorio; por tanto, la web no está
  conectada al pipeline actual.

## Requisitos e instalación

El proyecto declara Python `>=3.12,<3.13` en `pyproject.toml`.

Desde la raíz del repositorio, en PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[indexing,chunking]"
```

La instalación editable registra los paquetes que están en `src/` para que
puedan importarse directamente como `indexing`, `retrieval`, etc. El extra
`indexing` instala las dependencias de embeddings y Qdrant; `chunking` instala
las dependencias para ejecutar la estrategia JSON. PyTorch puede requerir una
instalación específica según el hardware y la versión de CUDA.

`pyproject.toml` es el archivo estándar de configuración y metadatos de un
proyecto Python: aquí declara el nombre, la versión, las dependencias, la
versión de Python y cómo descubrir los paquetes de `src/`.

## Indexación

Antes de indexar:

1. Configura `embeddings.model_name` y la conexión/colección de Qdrant en
   `configs/indexing_config.json`.
2. Coloca chunks en `data/processed/chunks/` o cambia `paths.chunks_dir`.
   Se admiten JSONL canónicos (un chunk por línea) y archivos JSON
   `chunk_results`.
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

## Chunking JSON

La estrategia jerárquica procesa los JSON de `data/processed/json` y escribe
los resultados bajo `data/processed/chunks/json/`, una ubicación que ya recorre
la normalización de indexación. Las rutas y los parámetros del tokenizador se
configuran en `configs/chunker_config.json`; las rutas relativas se resuelven
desde la raíz del repositorio.

```powershell
python scripts\run_chunking.py
```

Para ajustar el paralelismo o desactivar la caché:

```powershell
python scripts\run_chunking.py --workers 2 --no-cache
```

Esta integración selecciona únicamente la estrategia JSON. Las estrategias
Markdown del benchmark quedan fuera de este cambio.

El retrieval toma su límite de resultados de `configs/retrieval_config.json`.
También se puede usar desde Python:

```python
from retrieval.retriever import Retriever

with Retriever() as retriever:
    hits = retriever.retrieve("¿Cuál es la recomendación?", top_k=5)
```

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
  retrieval/   Búsqueda y construcción de contexto
tests/         Pruebas unitarias
web/           Prototipo Django aún no integrado con el pipeline
```
