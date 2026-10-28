"""Carga el modelo de embeddings y convierte textos en vectores por lotes."""

from __future__ import annotations

import os
from typing import Any, Iterator

import numpy as np
import torch
from langchain_huggingface import HuggingFaceEmbeddings


def create_embedder(
    config: dict[str, Any],
    device_override: str | None = None,
) -> HuggingFaceEmbeddings:
    # Se exige configurar el modelo para evitar indexar accidentalmente con
    # uno distinto al que se usará después para representar las preguntas.
    model_name = config.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError(
            "Configura embeddings.model_name en configs/indexing_config.json "
            "antes de indexar o recuperar."
        )

    # 'device' puede ser 'cpu' o, si PyTorch/CUDA están disponibles,
    # por ejemplo 'cuda:0'. Esta implementación carga un modelo/dispositivo.
    model_kwargs: dict[str, Any] = {
        "device": device_override or config.get("device", "cpu")
    }
    # Algunos modelos requieren ejecutar código publicado junto al modelo.
    # No se activa automáticamente por proveedor: habilitarlo solo tras revisar
    # y confiar explícitamente en el código del modelo elegido.
    model_kwargs["trust_remote_code"] = bool(config.get("trust_remote_code", False))

    # Indexación y consultas deben compartir la normalización de vectores.
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs=model_kwargs,
        encode_kwargs={
            "normalize_embeddings": config.get("normalize_embeddings", True)
        },
    )


def resolve_worker_config(
    config: dict[str, Any],
) -> tuple[int, int, list[int]]:
    """Valida workers e GPUs y devuelve workers CPU, hilos y GPUs seleccionadas."""
    num_workers = config.get("num_workers", 1)
    cpu_threads = config.get("cpu_threads", 1)
    num_gpus = config.get("num_gpus", 0)
    gpu_ids = config.get("gpu_ids", [])

    for name, value, minimum in (
        ("num_workers", num_workers, 1),
        ("cpu_threads", cpu_threads, 1),
        ("num_gpus", num_gpus, 0),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"indexing.{name} debe ser un entero >= {minimum}.")

    if not isinstance(gpu_ids, list) or any(
        not isinstance(gpu_id, int) or isinstance(gpu_id, bool) or gpu_id < 0
        for gpu_id in gpu_ids
    ):
        raise ValueError("indexing.gpu_ids debe ser una lista de enteros no negativos.")
    if len(gpu_ids) != len(set(gpu_ids)):
        raise ValueError("No se pueden repetir los valores de indexing.gpu_ids.")
    if num_gpus == 0 and gpu_ids:
        raise ValueError("indexing.gpu_ids requiere un valor positivo en num_gpus.")
    if num_gpus > 0:
        if gpu_ids and len(gpu_ids) != num_gpus:
            raise ValueError(
                "La cantidad de indexing.gpu_ids debe coincidir con indexing.num_gpus."
            )
        if not gpu_ids:
            gpu_ids = list(range(num_gpus))
        if not torch.cuda.is_available():
            raise RuntimeError(
                "Se han solicitado GPUs, pero CUDA no está disponible."
            )
        available_count = torch.cuda.device_count()
        invalid_ids = [gpu_id for gpu_id in gpu_ids if gpu_id >= available_count]
        if invalid_ids:
            raise ValueError(
                f"GPU IDs fuera de rango: {invalid_ids}; "
                f"hay {available_count} GPU(s) CUDA disponibles."
            )
    return num_workers, cpu_threads, gpu_ids


def _get_sentence_transformer(embedder: HuggingFaceEmbeddings) -> Any:
    """Obtiene el SentenceTransformer necesario para su pool multiproceso."""
    model = getattr(embedder, "client", None)
    if model is None:
        model = getattr(embedder, "_client", None)
    if model is None:
        raise AttributeError(
            "No se pudo acceder al SentenceTransformer interno de "
            "HuggingFaceEmbeddings para iniciar los workers."
        )
    return model


def iter_embedding_batches(
    embedder: HuggingFaceEmbeddings,
    texts: list[str],
    embedding_batch_size: int,
    cache_batch_size: int,
    num_workers: int,
    cpu_threads: int,
    gpu_ids: list[int],
) -> Iterator[tuple[int, list[list[float]]]]:
    """Genera lotes cacheables manteniendo un único pool durante la ejecución."""
    if not texts:
        return
    if embedding_batch_size < 1 or cache_batch_size < 1:
        raise ValueError("embedding_batch_size debe ser al menos 1.")
    parallel = bool(gpu_ids) or num_workers > 1
    if not parallel:
        previous_threads = torch.get_num_threads()
        try:
            torch.set_num_threads(cpu_threads)
            for cache_start in range(0, len(texts), cache_batch_size):
                cache_texts = texts[cache_start : cache_start + cache_batch_size]
                vectors = embed_documents(
                    embedder,
                    cache_texts,
                    batch_size=embedding_batch_size,
                )
                yield cache_start, vectors
        finally:
            torch.set_num_threads(previous_threads)
        return

    model = _get_sentence_transformer(embedder)
    target_devices = (
        [f"cuda:{gpu_id}" for gpu_id in gpu_ids]
        if gpu_ids
        else ["cpu"] * num_workers
    )
    thread_env = ("OMP_NUM_THREADS", "MKL_NUM_THREADS")
    previous_thread_env = {name: os.environ.get(name) for name in thread_env}
    previous_threads = torch.get_num_threads()
    pool = None
    try:
        # Los procesos heredan estos límites al arrancar y evitan competir
        # entre sí por todos los hilos de CPU disponibles.
        for name in thread_env:
            os.environ[name] = str(cpu_threads)
        torch.set_num_threads(cpu_threads)
        pool = model.start_multi_process_pool(target_devices=target_devices)
        for cache_start in range(0, len(texts), cache_batch_size):
            cache_texts = texts[cache_start : cache_start + cache_batch_size]
            vectors: list[list[float]] = []
            for start in range(0, len(cache_texts), embedding_batch_size):
                batch_vectors = model.encode_multi_process(
                    cache_texts[start : start + embedding_batch_size],
                    pool,
                    batch_size=embedding_batch_size,
                )
                vectors.extend(np.asarray(batch_vectors).tolist())
            if len(vectors) != len(cache_texts):
                raise RuntimeError(
                    "La cantidad de embeddings no coincide con el lote."
                )
            if embedder.encode_kwargs.get("normalize_embeddings", True):
                matrix = np.asarray(vectors)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                vectors = (matrix / norms).tolist()
            yield cache_start, vectors
    finally:
        try:
            if pool is not None:
                model.stop_multi_process_pool(pool)
        finally:
            torch.set_num_threads(previous_threads)
            for name, value in previous_thread_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def embed_documents(
    embedder: HuggingFaceEmbeddings,
    texts: list[str],
    prefix: str = "",
    batch_size: int = 32,
) -> list[list[float]]:
    # Los lotes limitan el uso de memoria GPU/CPU, sin abrir procesos workers.
    if batch_size < 1:
        raise ValueError("embedding_batch_size debe ser al menos 1.")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        # Algunos modelos de embeddings esperan prefijos distintos para
        # documentos y consultas; ambos se definen en la configuración.
        batch = [prefix + text for text in texts[start : start + batch_size]]
        vectors.extend(embedder.embed_documents(batch))
    # Comprobar el recuento evita asociar silenciosamente vectores incorrectos.
    if len(vectors) != len(texts):
        raise RuntimeError("La cantidad de embeddings no coincide con los textos.")
    return vectors
