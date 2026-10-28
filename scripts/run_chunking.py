"""Lanzador CLI para el chunking jerárquico JSON."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from chunking.run_JSON import cli


if __name__ == "__main__":
    cli()
