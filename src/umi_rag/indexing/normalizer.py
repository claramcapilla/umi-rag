"""Convierte las entradas de chunks admitidas a un esquema JSONL común."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


def _normalise_record(
    chunk: dict[str, Any],
    document: dict[str, Any] | None = None,
    chunk_index: int | None = None,
    source_file: Path | None = None,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    relative_source = (
        source_file.relative_to(source_dir).as_posix()
        if source_file is not None and source_dir is not None
        else (source_file.name if source_file is not None else "unknown")
    )
    source_key = hashlib.sha256(relative_source.encode("utf-8")).hexdigest()[:16]

    # JSONL canónico: conservar los campos adicionales definidos por quien
    # produzca los chunks, validando los mínimos que necesita la indexación.
    if document is None:
        record = dict(chunk)
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Cada chunk debe incluir un campo 'text' no vacío.")
        chunk_id = record.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            record["chunk_id"] = f"source-{source_key}-{chunk_index}"
        record.setdefault("metadata", {})
        record.setdefault("source_file", relative_source)
        record.setdefault("document_id", f"source-{source_key}")
        return record

    # Formato actual del benchmark: extraer los datos del documento y del
    # chunk para que el retrieval pueda devolver referencias útiles.
    text = chunk.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Chunk {chunk_index} sin texto válido: {source_file}")

    document_id = str(document.get("doc_id", f"source-{source_key}"))
    strategy = str(document.get("strategy", "unknown"))
    format_name = next(
        (
            part
            for part in reversed(source_file.parts)
            if part.lower() in {"json", "markdown"}
        ),
        "unknown",
    )
    abbreviation = str(document.get("abbreviation", "unknown"))
    metadata = dict(chunk.get("metadata") or {})
    # Los runners JSON pueden guardar metadatos estructurales directamente
    # en el chunk, no dentro de su objeto metadata.
    for key in (
        "block",
        "group_name",
        "level",
        "json_paths",
        "exceeds_target_limit",
        "exceeds_max_limit",
    ):
        if key in chunk:
            metadata[key] = chunk[key]
    if chunk.get("chunk_id") is not None:
        metadata["original_chunk_id"] = chunk["chunk_id"]

    # El ID normalizado es determinista para que una reindexación actualice
    # el mismo punto de Qdrant; el ID original se conserva en metadata.
    return {
        "chunk_id": (
            f"{format_name}__{document_id}__{abbreviation}__{chunk_index:06d}"
        ),
        "text": text,
        "format": format_name,
        "document_type": str(document.get("document_type", "unknown")),
        "document_id": document_id,
        "source_file": document.get("input_file", source_file.name),
        "source_path": document.get("source_path"),
        "strategy": strategy,
        "strategy_abbreviation": abbreviation,
        "strategy_params": document.get("params", {}),
        "chunk_index": chunk_index,
        "token_count": chunk.get("token_count"),
        "metadata": metadata,
    }


def iter_normalized_chunks(source_dir: Path) -> Iterator[dict[str, Any]]:
    """Recorre JSONL canónicos y JSON chunk_results del benchmark."""
    if not source_dir.is_dir():
        raise FileNotFoundError(f"No existe la carpeta de chunks: {source_dir}")

    files = sorted(source_dir.rglob("*.jsonl"))
    files.extend(sorted(source_dir.rglob("*.json")))
    found = False

    # Se admiten los dos formatos de entrada para no acoplar esta etapa al
    # formato final que se elija para el chunking.
    for path in files:
        if path.suffix == ".jsonl":
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError(f"Registro no válido en {path}:{line_number}")
                    found = True
                    yield _normalise_record(
                        record,
                        chunk_index=line_number - 1,
                        source_file=path,
                        source_dir=source_dir,
                    )
            continue

        with path.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict) or not isinstance(document.get("chunks"), list):
            continue
        found = True
        for index, chunk in enumerate(document["chunks"]):
            if not isinstance(chunk, dict):
                raise ValueError(f"Chunk {index} no es un objeto JSON: {path}")
            yield _normalise_record(chunk, document, index, path, source_dir)

    if not found:
        raise FileNotFoundError(
            f"No se encontraron chunks JSONL ni archivos chunk_results JSON en {source_dir}"
        )


def write_normalized_chunks(source_dir: Path, output_file: Path) -> int:
    # Escribir a JSONL permite procesar registros individualmente y facilita
    # inspeccionar o reanudar fases posteriores.
    output_file.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_file.open("w", encoding="utf-8") as stream:
        for chunk in iter_normalized_chunks(source_dir):
            stream.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            count += 1
    return count


def load_normalized_chunks(path: Path) -> list[dict[str, Any]]:
    # La indexación carga los registros ya normalizados y falla ante datos
    # incompletos en lugar de omitirlos silenciosamente.
    if not path.is_file():
        raise FileNotFoundError(f"No existe el archivo normalizado: {path}")
    chunks = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            chunk = json.loads(line)
            if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str):
                raise ValueError(f"Chunk normalizado no válido en {path}:{line_number}")
            chunks.append(chunk)
    if not chunks:
        raise ValueError(f"No hay chunks para indexar en {path}")
    return chunks
