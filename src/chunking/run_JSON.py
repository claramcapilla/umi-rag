# ============================================================
# CHUNKING JSON
# ============================================================

"""
Ejecuta el chunking jerárquico para documentos JSON estructurados.

Este script:
1. Carga los documentos JSON desde data/processed/json.
2. Detecta automáticamente la tipología documental (paper, guideline,
   ficha técnica) según la estructura y los campos del JSON.
3. Selecciona el document plan correspondiente.
4. Aplica el HierarchicalJSONChunker, que:
   - respeta la estructura del JSON,
   - evita cortes peligrosos,
   - genera chunks semánticos y trazables,
   - añade metadatos como nivel, grupo, rutas JSON y límites de tokens.
5. Guarda los chunks en
   la ruta de salida configurada.
6. Guarda las métricas y el resumen junto a los chunks.
7. Genera un summary.csv con una fila por documento.
"""


# ============================================================
# IMPORTS GENERALES
# ============================================================

import json
import os
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import psutil

# Utilidades
from chunking.utilities import (
    calculate_stats,
    create_tokenizer,
    create_token_counter,
    save_json,
)
from chunking.chunk_cache import cache_fingerprint, load_cached_artifacts
from chunking.config import load_config, resolve_config_path

# ============================================================
# JSON-CHUNKER
# ============================================================

from chunking.chunkers.json import (
    DOCUMENT_PLAN_FC,
    DOCUMENT_PLAN_GL,
    DOCUMENT_PLAN_PAPER,
    HierarchicalJSONChunker,
)

# ============================================================
# RUTAS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "chunker_config.json"

# ============================================================
# SELECCIÓN DEL DOCUMENT PLAN
# ============================================================

DOCUMENT_PLANS = {
    "paper": DOCUMENT_PLAN_PAPER,
    "guideline": DOCUMENT_PLAN_GL,
    "ficha_tecnica": DOCUMENT_PLAN_FC,
}


def select_document_plan(document_type: str):
    try:
        return DOCUMENT_PLANS[document_type]
    except KeyError as error:
        available_types = ", ".join(DOCUMENT_PLANS)
        raise ValueError(
            f"Tipo de documento no soportado: {document_type!r}. "
            f"Tipos disponibles: {available_types}"
        ) from error


FICHA_TECNICA_SIGNATURE = {
    "medicine_name",
    "composition",
    "pharmaceutical_form",
    "indications",
    "posology",
}


def detect_document_type(
    data: Dict[str, Any],
    source_path: str,
) -> str:
    keys = set(data)
    if FICHA_TECNICA_SIGNATURE.issubset(keys):
        return "ficha_tecnica"

    declared_type = data.get("document_type")
    if isinstance(declared_type, str) and declared_type.strip():
        normalized_type = declared_type.strip().casefold().replace("-", "_")
        if normalized_type in {"guideline", "clinical_guideline"}:
            if data.get("article_type"):
                raise ValueError(
                    f"El documento {source_path!r} contiene señales "
                    "contradictorias: document_type y article_type."
                )
            return "guideline"
        raise ValueError(
            f"Tipo de documento no soportado en {source_path!r}: "
            f"{declared_type!r}."
        )

    article_type = data.get("article_type")
    if isinstance(article_type, str) and article_type.strip():
        return "paper"

    raise ValueError(
        f"No se pudo detectar el tipo de documento para {source_path!r}. "
        "Se esperaba la firma de ficha técnica, document_type de guideline "
        "o article_type de paper."
    )


# INVIDISIBLE LIST PATHS

INDIVISIBLE_LIST_PATHS = {

    "paper": ["authors", "keywords"],

    "guideline": ["toc", "references", "abbreviations", "metadata.authors"],

    "ficha_tecnica": ["excipients","national_codes","national_codes_queried"],
}

# ============================================================
# PARALELIZACIÓN DE DOCUMENTOS
# ============================================================

_WORKER_CONTEXT = threading.local()


def _initialize_worker(
    model_name: str,
    add_special_tokens: bool,
) -> None:
    tokenizer = create_tokenizer(model_name)
    _WORKER_CONTEXT.token_counter = create_token_counter(
        tokenizer,
        add_special_tokens=add_special_tokens,
    )


def _process_document(
    doc: Dict[str, Any],
    target_tokens: int,
    max_tokens: int,
    measure_ram: bool,
) -> Dict[str, Any]:
    token_counter = _WORKER_CONTEXT.token_counter
    original_token_count = token_counter(doc["full_text"])
    chunker = HierarchicalJSONChunker(
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        token_counter=token_counter,
        document_plan=select_document_plan(doc["document_type"]),
        root_field=None,
        indivisible_list_paths=INDIVISIBLE_LIST_PATHS[doc["document_type"]],
    )

    process = psutil.Process(os.getpid()) if measure_ram else None
    ram_before = (
        process.memory_info().rss / (1024 ** 2)
        if process is not None
        else None
    )
    start = time.perf_counter()
    chunks = chunker.chunk_document(doc["data"])
    execution_time = time.perf_counter() - start
    ram_after = (
        process.memory_info().rss / (1024 ** 2)
        if process is not None
        else None
    )

    stats = calculate_stats(
        chunks=chunks,
        original_token_count=original_token_count,
        execution_time_seconds=execution_time,
        ram_before_mb=ram_before if ram_before is not None else 0.0,
        ram_after_mb=ram_after if ram_after is not None else 0.0,
        token_counter=token_counter,
    )
    if ram_before is None or ram_after is None:
        stats["ram_before_mb"] = None
        stats["ram_after_mb"] = None
        stats["ram_increase_mb"] = None

    return {
        "chunks": chunks,
        "original_token_count": original_token_count,
        "stats": stats,
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "El número de workers debe ser un entero positivo."
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "El número de workers debe ser al menos 1."
        )
    return parsed

# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def main(
    workers_override: int | None = None,
    use_cache: bool = True,
    config_file: Path = DEFAULT_CONFIG,
) -> None:
    print("=" * 70)
    print("INICIO DE LA EJECUCIÓN (JSON)")
    print("=" * 70)

    project_root = config_file.resolve().parent.parent
    chunker_config = load_config(config_file)
    paths = chunker_config.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("La configuración debe incluir el objeto 'paths'.")
    input_dir = resolve_config_path(
        project_root,
        paths.get("input_dir"),
        "paths.input_dir",
    )
    output_dir = resolve_config_path(
        project_root,
        paths.get("output_dir"),
        "paths.output_dir",
    )
    if not input_dir.is_dir():
        raise FileNotFoundError(f"No existe la carpeta de entrada JSON: {input_dir}")

    tokenizer_cfg = chunker_config.get("tokenizer")
    if not isinstance(tokenizer_cfg, dict):
        raise ValueError("La configuración debe incluir el objeto 'tokenizer'.")
    model_name = tokenizer_cfg.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("'tokenizer.model_name' debe ser una cadena no vacía.")
    add_special_tokens = tokenizer_cfg.get("add_special_tokens", True)
    if not isinstance(add_special_tokens, bool):
        raise ValueError("'tokenizer.add_special_tokens' debe ser booleano.")
    target_tokens = tokenizer_cfg.get("target_tokens")
    max_tokens = tokenizer_cfg.get("max_tokens")
    if (
        not isinstance(target_tokens, int)
        or isinstance(target_tokens, bool)
        or target_tokens < 1
    ):
        raise ValueError("'tokenizer.target_tokens' debe ser un entero positivo.")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
    ):
        raise ValueError("'tokenizer.max_tokens' debe ser un entero positivo.")
    if target_tokens > max_tokens:
        raise ValueError("'tokenizer.target_tokens' no puede superar 'max_tokens'.")

    execution_config = chunker_config.get("execution", {})
    if not isinstance(execution_config, dict):
        raise ValueError("'execution' debe ser un objeto.")
    configured_workers = execution_config.get("workers", 1)
    if (
        not isinstance(configured_workers, int)
        or isinstance(configured_workers, bool)
        or configured_workers < 1
    ):
        raise ValueError("'execution.workers' debe ser un entero positivo.")
    requested_workers = (
        workers_override if workers_override is not None else configured_workers
    )
    if (
        not isinstance(requested_workers, int)
        or isinstance(requested_workers, bool)
        or requested_workers < 1
    ):
        raise ValueError("El número de workers debe ser un entero positivo.")

    chunks_dir = output_dir / "json" / "chunk_results"
    metrics_dir = output_dir / "json" / "metrics"
    summary_file = output_dir / "json" / "summary.csv"

    # 1. Cargar JSON
    json_files = sorted(
        p for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() == ".json"
    )
    if not json_files:
        raise FileNotFoundError(
            f"No se encontraron archivos JSON en la carpeta de entrada: {input_dir}"
        )

    documents = []
    for path in json_files:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        rel = path.relative_to(input_dir)
        source_path = rel.as_posix()
        if not isinstance(data, dict):
            raise TypeError(
                f"El documento JSON {source_path!r} debe ser un objeto."
            )
        doc_type = detect_document_type(data, source_path)
        doc_id = "_".join(rel.with_suffix("").parts)

        full_text = json.dumps(data, ensure_ascii=False)

        documents.append({
            "doc_id": doc_id,
            "document_type": doc_type,
            "file_name": path.name,
            "source_path": source_path,
            "data": data,
            "full_text": full_text,
        })

    # La estrategia JSON elegida es la única que ejecuta este runner.
    strategy_name = "JSONChunking"
    abbreviation = "JC"

    summary_results = []
    active_doc_ids = {doc["doc_id"] for doc in documents}
    previous_summary = (
        pd.read_csv(summary_file, dtype={"doc_id": str})
        if summary_file.is_file()
        else pd.DataFrame()
    )
    if "doc_id" in previous_summary.columns:
        retained_summary = previous_summary[
            ~previous_summary["doc_id"].astype(str).isin(active_doc_ids)
        ]
    else:
        retained_summary = previous_summary

    pending_documents = []
    cache_fingerprints = {}
    for doc in documents:
        fingerprint = cache_fingerprint({
            "version": 1,
            "chunker": "HierarchicalJSONChunker",
            "document_type": doc["document_type"],
            "content": doc["full_text"],
            "tokenizer_model": model_name,
            "add_special_tokens": add_special_tokens,
            "target_tokens": target_tokens,
            "max_tokens": max_tokens,
        })
        cache_fingerprints[doc["doc_id"]] = fingerprint
        chunks_path = chunks_dir / f"{abbreviation}_{doc['doc_id']}.json"
        metrics_path = (
            metrics_dir / f"{abbreviation}_{doc['doc_id']}_metrics.json"
        )
        cached = (
            load_cached_artifacts(chunks_path, metrics_path, fingerprint)
            if use_cache
            else None
        )
        if cached is None:
            pending_documents.append(doc)
            continue

        _, metrics = cached
        summary_results.append({
            key: value
            for key, value in metrics.items()
            if key != "_cache_fingerprint"
        })
        print(f"\nCaché reutilizada: {doc['file_name']}")

    available_cpus = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    workers = min(
        requested_workers,
        available_cpus,
        max(1, len(pending_documents)),
    )
    print(f"Workers para chunking JSON: {workers}")

    # 4. Ejecutar chunking JSON
    if pending_documents and workers == 1:
        _initialize_worker(model_name, add_special_tokens)
        document_results = (
            _process_document(doc, target_tokens, max_tokens, True)
            for doc in pending_documents
        )
        results = zip(pending_documents, document_results)
        for doc, processed in results:
            _save_document_result(
                doc,
                processed,
                model_name,
                target_tokens,
                max_tokens,
                strategy_name,
                abbreviation,
                cache_fingerprints[doc["doc_id"]],
                summary_results,
                chunks_dir,
                metrics_dir,
            )
    elif pending_documents:
        with ThreadPoolExecutor(
            max_workers=workers,
            initializer=_initialize_worker,
            initargs=(model_name, add_special_tokens),
            thread_name_prefix="json-chunker",
        ) as executor:
            for doc, processed in zip(
                pending_documents,
                executor.map(
                    lambda document: _process_document(
                        document,
                        target_tokens,
                        max_tokens,
                        False,
                    ),
                    pending_documents,
                ),
            ):
                _save_document_result(
                    doc,
                    processed,
                    model_name,
                    target_tokens,
                    max_tokens,
                    strategy_name,
                    abbreviation,
                    cache_fingerprints[doc["doc_id"]],
                    summary_results,
                    chunks_dir,
                    metrics_dir,
                )

    # 5. Summary CSV
    summary_frames = [
        frame
        for frame in (
            retained_summary,
            pd.DataFrame(summary_results),
        )
        if not frame.empty
    ]
    if summary_frames:
        df = pd.concat(summary_frames, ignore_index=True)
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(summary_file, index=False, encoding="utf-8-sig")
    elif summary_file.is_file():
        retained_summary.to_csv(
            summary_file,
            index=False,
            encoding="utf-8-sig",
        )

    print("=" * 70)
    print("FIN DE LA EJECUCIÓN (JSON)")
    print("=" * 70)


def _save_document_result(
    doc: Dict[str, Any],
    processed: Dict[str, Any],
    model_name: str,
    target_tokens: int,
    max_tokens: int,
    strategy_name: str,
    abbreviation: str,
    fingerprint: str,
    summary_results: list[Dict[str, Any]],
    chunks_dir: Path,
    metrics_dir: Path,
) -> None:
    print(f"\nProcesando: {doc['file_name']}")
    chunks = processed["chunks"]
    serialized = [asdict(chunk) for chunk in chunks]
    params = {
        "target_tokens": target_tokens,
        "max_tokens": max_tokens,
    }

    result = {
        "strategy": strategy_name,
        "type": "HierarchicalJSONChunker",
        "abbreviation": abbreviation,
        "doc_id": doc["doc_id"],
        "document_type": doc["document_type"],
        "input_file": doc["file_name"],
        "source_path": doc["source_path"],
        "tokenizer_model": model_name,
        "params": params,
        "original_token_count": processed["original_token_count"],
        "number_of_chunks": len(chunks),
        "chunks": serialized,
        "_cache_fingerprint": fingerprint,
    }

    save_json(result, chunks_dir / f"{abbreviation}_{doc['doc_id']}.json")

    metrics = {
        "strategy": strategy_name,
        "type": "HierarchicalJSONChunker",
        "abbreviation": abbreviation,
        "status": "completed",
        "doc_id": doc["doc_id"],
        "document_type": doc["document_type"],
        "input_file": doc["file_name"],
        "source_path": doc["source_path"],
        "tokenizer_model": model_name,
        "params": params,
        **processed["stats"],
        "_cache_fingerprint": fingerprint,
    }

    save_json(
        metrics,
        metrics_dir / f"{abbreviation}_{doc['doc_id']}_metrics.json",
    )
    summary_results.append(metrics)


# ============================================================
# PUNTO DE ENTRADA
# ============================================================

def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Ejecuta el chunking jerárquico de documentos JSON."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Ruta a chunker_config.json.",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        help="Máximo de documentos procesados en paralelo (por defecto, 1).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Fuerza el reprocesamiento de todos los documentos.",
    )
    arguments = parser.parse_args()
    main(
        workers_override=arguments.workers,
        use_cache=not arguments.no_cache,
        config_file=arguments.config,
    )


if __name__ == "__main__":
    cli()
