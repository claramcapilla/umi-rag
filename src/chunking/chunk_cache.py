"""Funciones auxiliares para validar resultados de chunking guardados."""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Tuple


def cache_fingerprint(value: Any) -> str:
    """Crea una huella estable para datos de caché compatibles con JSON."""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_cached_artifacts(
    chunks_path: Path,
    metrics_path: Path,
    fingerprint: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]] | None:
    """Devuelve los archivos de chunks y métricas coincidentes, si existen."""
    if not chunks_path.is_file() or not metrics_path.is_file():
        return None

    try:
        with chunks_path.open("r", encoding="utf-8") as file:
            chunks_result = json.load(file)
        with metrics_path.open("r", encoding="utf-8") as file:
            metrics_result = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(
            f"[AVISO] Caché ilegible; se volverá a procesar "
            f"{chunks_path.name}: {error}"
        )
        return None

    if (
        not isinstance(chunks_result, dict)
        or not isinstance(metrics_result, dict)
        or chunks_result.get("_cache_fingerprint") != fingerprint
        or metrics_result.get("_cache_fingerprint") != fingerprint
        or not isinstance(chunks_result.get("chunks"), list)
        or chunks_result.get("number_of_chunks")
        != len(chunks_result.get("chunks", []))
        or not isinstance(metrics_result.get("original_token_count"), int)
        or isinstance(metrics_result.get("original_token_count"), bool)
        or metrics_result.get("original_token_count") < 0
        or chunks_result.get("original_token_count")
        != metrics_result.get("original_token_count")
        or chunks_result.get("doc_id") != metrics_result.get("doc_id")
        or chunks_result.get("strategy") != metrics_result.get("strategy")
        or metrics_result.get("status") != "completed"
    ):
        return None

    chunks_result.pop("_cache_fingerprint", None)
    metrics_result.pop("_cache_fingerprint", None)
    return chunks_result, metrics_result
