"""Lanzador CLI para normalizar e indexar chunks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Permite ejecutar el lanzador desde el repositorio sin instalar el paquete.
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from indexing.pipeline import DEFAULT_CONFIG, normalize_only, run_indexing


def main() -> None:
    # Exponer una opción para normalizar/inspeccionar chunks sin cargar
    # embeddings ni necesitar un servidor Qdrant activo.
    parser = argparse.ArgumentParser(description="Normaliza e indexa chunks en Qdrant.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Ruta a indexing_config.json.",
    )
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Normaliza chunks sin crear embeddings ni conectar a Qdrant.",
    )
    args = parser.parse_args()
    count = (
        normalize_only(args.config)
        if args.normalize_only
        else run_indexing(args.config)
    )
    print(f"Proceso completado: {count} chunks.")


if __name__ == "__main__":
    main()
