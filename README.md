# umi-rag: piezas provisionales de indexación y retrieval

Esta carpeta es un área de preparación para trasladar al futuro repositorio
`umi-rag`. No modifica el benchmark ni depende del código de `chunking/`.
Elimina la carpeta cuando ya no necesites esta preparación.

## Antes de ejecutar

1. En `configs/indexing_config.json`, elige el modelo definitivo de embeddings
   y configura la conexión/colección de Qdrant. El nombre del modelo se deja
   sin definir intencionadamente; indexación y retrieval leen siempre el mismo
   valor de esta configuración.
2. Coloca la salida de chunks bajo `data/processed/chunks/` o cambia
   `paths.chunks_dir`. Se admiten tanto JSONL canónicos (un chunk por línea)
   como archivos JSON `chunk_results` producidos por los runners actuales.
3. Instala las dependencias del proyecto que correspondan (`qdrant-client`,
   `langchain-huggingface`, `sentence-transformers` y `torch`) en el futuro
   repositorio y arranca Qdrant.

Desde la raíz de esta carpeta:

```powershell
python scripts\run_indexing.py
```

El lanzador llama a `indexing.pipeline`: normaliza los chunks, reutiliza o
calcula sus embeddings y hace upsert en una única colección. También acepta
`--normalize-only` para probar la lectura/normalización sin cargar el modelo ni
conectar a Qdrant. Si la colección ya existe, la conserva (no la borra) y falla
si su dimensión no coincide con la del modelo.

### Añadir documentos después de la primera indexación

La indexación está pensada para ser incremental y repetible. Cuando la futura
ingesta convierta un PDF nuevo a chunks, basta con dejar su salida en
`paths.chunks_dir` y volver a ejecutar el lanzador; no hay que cambiar la
configuración ni volver a crear la colección. El pipeline vuelve a leer el
conjunto de chunks para validar/normalizarlo, pero la caché evita recalcular los
embeddings existentes y Qdrant solo recibe puntos nuevos o cuyo payload haya
cambiado. Los IDs deterministas hacen que una reejecución no duplique puntos.

Para que funcione, cada chunk debe mantener el mismo `chunk_id` entre
ejecuciones. Los formatos canónicos sin ID reciben uno determinista a partir
de la ruta relativa del archivo y el número de línea; los resultados actuales
del benchmark conservan una identidad basada en documento, estrategia e
índice. La carpeta preparada aún no convierte PDFs ni registra versiones:
esa futura etapa de ingesta tendrá que producir los chunks. El borrado de
documentos, o la sustitución de un documento cuyo esquema de IDs haya cambiado,
no se sincroniza automáticamente con Qdrant; se resolverá con el futuro
registro de documentos y una política explícita de actualización/eliminación.

## Qué hace cada pieza

- `indexing.normalizer`: acepta JSONL canónico o los archivos
  `chunk_results` del benchmark y los convierte a una representación común.
  No decide cómo se crean los chunks.
- `indexing.embedder`: carga el modelo configurado, aplica prefijos opcionales
  y calcula los vectores por lotes.
- `indexing.embedding_cache`: guarda los vectores por modelo, ID y hash del
  texto, y recalcula solo lo que falta o ha cambiado.
- `indexing.qdrant_store`: conecta con Qdrant, crea la colección si no existe y
  actualiza los puntos con IDs deterministas. No elimina colecciones.
- `indexing.pipeline`: ejecuta estas fases en orden.
- `retrieval.retriever`: recibe una pregunta individual, la vectoriza con el
  mismo modelo y busca los chunks más similares. Devuelve texto y metadatos;
  no crea el prompt, no llama a un LLM y no redacta la respuesta.

El retrieval de este RAG no necesita un archivo de preguntas: el servicio del
chatbot le pasará el texto de cada turno directamente. Se puede reutilizar el
modelo y la conexión para una conversación:

```python
from umi_rag.retrieval.retriever import Retriever

with Retriever() as retriever:
    hits = retriever.retrieve("¿Cuál es la recomendación?", top_k=5)
```

Se conservan `num_workers`, `cpu_threads`, `num_gpus` y `gpu_ids`, con valores
por defecto seguros en `configs/indexing_config.json`. En CPU, `num_workers`
controla los procesos y `cpu_threads` limita los hilos de cada uno. Si se
configura `num_gpus` mayor que cero, cada GPU seleccionada recibe un worker;
`gpu_ids` es opcional y, si se omite, se usan los índices consecutivos desde
cero. La disponibilidad y los índices CUDA se validan antes de indexar. En
modo multiproceso, el modelo se carga en CPU y Sentence Transformers lo
distribuye a los workers. `embedding_batch_size` regula el trabajo de cada
lote y `cache_batch_size` cada punto de control de la caché. Para el modo
simple se puede dejar un worker, cero GPUs y `embeddings.device` en `cpu`.

`embeddings.trust_remote_code` está desactivado por defecto. Algunos modelos
pueden pedirlo para cargar código adicional publicado por sus autores; solo se
debe activar tras revisar y confiar en ese código. No se activa por el nombre
de un proveedor.

Para importar el paquete antes de empaquetar el proyecto, añade `src` a
`PYTHONPATH` o instala el paquete al crear el `pyproject.toml` del repositorio
definitivo.

El formato de chunking, el conversor PDF, la API y la generación del chatbot
siguen fuera de este alcance.
