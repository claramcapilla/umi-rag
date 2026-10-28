"""Caché JSONL de vectores para evitar recalcular chunks sin cambios."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from indexing.embedder import iter_embedding_batches


def _text_hash(text: str) -> str:
    # El hash permite invalidar la caché si cambia el texto o su prefijo,
    # incluso aunque se conserve el mismo chunk_id.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, model_name: str) -> Path:
    # El modelo forma parte de la ruta para no mezclar espacios vectoriales.
    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", model_name).strip("_")
    return cache_dir / f"{safe_name}.jsonl"


def load_cache(cache_dir: Path, model_name: str) -> dict[str, dict[str, Any]]:
    path = _cache_path(cache_dir, model_name)
    if not path.exists():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    valid_bytes = 0
    # Leer en binario permite detectar y descartar una última línea incompleta
    # si la ejecución anterior se interrumpió durante la escritura.
    with path.open("rb") as stream:
        for line in stream:
            if not line.strip():
                valid_bytes += len(line)
                continue
            try:
                record = json.loads(line)
                cache[record["chunk_id"]] = {
                    "text_hash": record["text_hash"],
                    "vector": record["vector"],
                }
            except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
                if not line.endswith(b"\n"):
                    break
                raise
            valid_bytes += len(line)
    if valid_bytes < path.stat().st_size:
        with path.open("r+b") as stream:
            stream.truncate(valid_bytes)
    return cache


def save_cache_entries(
    cache_dir: Path,
    model_name: str,
    entries: list[tuple[str, str, list[float]]],
) -> None:
    # Se añade un registro por vector para que las entradas ya guardadas
    # sobrevivan a una interrupción del proceso.
    if not entries:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, model_name)
    with path.open("a", encoding="utf-8") as stream:
        for chunk_id, text_hash, vector in entries:
            stream.write(
                json.dumps(
                    {"chunk_id": chunk_id, "text_hash": text_hash, "vector": vector}
                )
                + "\n"
            )
        stream.flush()


def embed_chunks_with_cache(
    embedder: Any,
    chunks: list[dict[str, Any]],
    model_name: str,
    cache_dir: Path,
    prefix: str,
    batch_size: int,
    use_cache: bool,
    num_workers: int = 1,
    cpu_threads: int = 1,
    gpu_ids: list[int] | None = None,
    embedding_batch_size: int | None = None,
) -> list[list[float]]:
    # Mantener el orden original de chunks: Qdrant recibirá cada vector junto
    # al payload del chunk correspondiente.
    cache = load_cache(cache_dir, model_name) if use_cache else {}
    vectors: list[list[float] | None] = [None] * len(chunks)
    pending: list[tuple[int, str, str]] = []
    for index, chunk in enumerate(chunks):
        text = prefix + chunk["text"]
        digest = _text_hash(text)
        cached = cache.get(str(chunk["chunk_id"]))
        if cached is not None and cached["text_hash"] == digest:
            vectors[index] = cached["vector"]
        else:
            pending.append((index, text, digest))

    # Solo calcular los vectores ausentes o cuyo texto/prefijo haya cambiado.
    # El pool de workers se mantiene vivo entre los lotes cacheables.
    pending_texts = [item[1] for item in pending]
    for start, new_vectors in iter_embedding_batches(
        embedder=embedder,
        texts=pending_texts,
        embedding_batch_size=embedding_batch_size or batch_size,
        cache_batch_size=batch_size,
        num_workers=num_workers,
        cpu_threads=cpu_threads,
        gpu_ids=gpu_ids or [],
    ):
        batch = pending[start : start + len(new_vectors)]
        if len(new_vectors) != len(batch):
            raise RuntimeError("La cantidad de embeddings no coincide con el lote.")
        entries = []
        for (index, _text, digest), vector in zip(batch, new_vectors):
            vectors[index] = vector
            entries.append((str(chunks[index]["chunk_id"]), digest, vector))
        if use_cache:
            save_cache_entries(cache_dir, model_name, entries)

    if any(vector is None for vector in vectors):
        raise RuntimeError("No se pudieron generar todos los embeddings.")
    return [vector for vector in vectors if vector is not None]
