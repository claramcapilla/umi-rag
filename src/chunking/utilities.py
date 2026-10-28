# ============================================================
# IMPORTS
# ============================================================

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import numpy as np
from transformers import AutoTokenizer


# ============================================================
# FUNCIÓN PARA GUARDAR ARCHIVOS JSON
# ============================================================

def save_json(
    data: Any,
    output_path: Path,
) -> None:
    """
    Guarda datos de Python en un archivo JSON.

    Parameters
    ----------
    data:
        Datos que se quieren guardar. Pueden ser un diccionario,
        una lista u otro objeto compatible con JSON.

    output_path:
        Ruta completa del archivo JSON de salida.
    """

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        with output_path.open(
            mode="w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )

    except TypeError as error:
        raise TypeError(
            f"No se han podido guardar los datos en "
            f"{output_path}. Algún objeto no es compatible "
            f"con el formato JSON."
        ) from error

    except OSError as error:
        raise OSError(
            f"No se ha podido escribir el archivo: "
            f"{output_path}"
        ) from error


# ============================================================
# FUNCIÓN PARA CREAR EL CONTADOR DE TOKENS
# ============================================================

def create_tokenizer(
    model_name: str,
):
    """
    Carga el tokenizer del modelo indicado.
    """

    tokenizer = AutoTokenizer.from_pretrained(
        model_name
    )

    return tokenizer

def create_token_counter(
    tokenizer,
    add_special_tokens: bool = True,
) -> Callable[[str], int]:
    """
    Crea un contador utilizando el tokenizer del modelo
    de embeddings.

    Parameters
    ----------
    model_name:
        Nombre del modelo de Hugging Face que se utilizará.

    add_special_tokens:
        Indica si se deben añadir tokens especiales al contar.
        Por defecto se conserva el comportamiento anterior.
    """

    def count_tokens(text: str) -> int:
        if not text or not text.strip():
            return 0

        return len(
            tokenizer.encode(
                text,
                add_special_tokens=add_special_tokens,
                truncation=False,
            )
        )

    return count_tokens


# ============================================================
# FUNCIÓN PARA CALCULAR ESTADÍSTICAS
# ============================================================

def calculate_stats(
    chunks: List[Any],
    original_token_count: int,
    execution_time_seconds: float,
    ram_before_mb: float,
    ram_after_mb: float,
    token_counter: Callable[[str], int],
) -> Dict[str, Any]:
    """
    Calcula estadísticas sobre el número de tokens de los chunks.

    Parameters
    ----------
    chunks:
        Lista de chunks producidos por una estrategia.

    original_token_count:
        Número total de tokens de los documentos originales.

    execution_time_seconds:
        Tiempo empleado por la estrategia.

    ram_before_mb:
        Memoria RAM utilizada antes de ejecutar la estrategia.

    ram_after_mb:
        Memoria RAM utilizada después de ejecutar la estrategia.

    token_counter:
        Función utilizada para contar tokens con el tokenizer
        del modelo de embeddings.

    Returns
    -------
    Dict[str, Any]
        Diccionario con las estadísticas calculadas.
    """

    valid_chunks = [
        chunk
        for chunk in chunks
        if chunk.text and chunk.text.strip()
    ]

    lengths = [
        token_counter(chunk.text.strip())
        for chunk in valid_chunks
    ]

    empty_chunks = len(chunks) - len(valid_chunks)

    if not lengths:
        return {
            "number_of_chunks": 0,
            "empty_chunks": empty_chunks,
            "tokens_min": 0,
            "tokens_p25": 0,
            "tokens_median": 0,
            "tokens_mean": 0,
            "tokens_p75": 0,
            "tokens_max": 0,
            "tokens_std": 0,
            "tokens_total": 0,
            "tokens_original": original_token_count,
            "tokens_ratio": 0,
            "execution_time_seconds": round(
                execution_time_seconds,
                6,
            ),
            "ram_before_mb": round(
                ram_before_mb,
                2,
            ),
            "ram_after_mb": round(
                ram_after_mb,
                2,
            ),
            "ram_increase_mb": round(
                ram_after_mb - ram_before_mb,
                2,
            ),
        }

    total_tokens = sum(lengths)

    return {
        "number_of_chunks": len(valid_chunks),
        "empty_chunks": empty_chunks,
        "tokens_min": min(lengths),
        "tokens_p25": round(
            float(np.percentile(lengths, 25)),
            2,
        ),
        "tokens_median": round(
            float(np.median(lengths)),
            2,
        ),
        "tokens_mean": round(
            float(np.mean(lengths)),
            2,
        ),
        "tokens_p75": round(
            float(np.percentile(lengths, 75)),
            2,
        ),
        "tokens_max": max(lengths),
        "tokens_std": round(
            float(np.std(lengths)),
            2,
        ),
        "tokens_total": total_tokens,
        "tokens_original": original_token_count,
        "tokens_ratio": round(
            total_tokens / original_token_count,
            3,
        )
        if original_token_count > 0
        else 0,
        "execution_time_seconds": round(
            execution_time_seconds,
            6,
        ),
        "ram_before_mb": round(
            ram_before_mb,
            2,
        ),
        "ram_after_mb": round(
            ram_after_mb,
            2,
        ),
        "ram_increase_mb": round(
            ram_after_mb - ram_before_mb,
            2,
        ),
    }
