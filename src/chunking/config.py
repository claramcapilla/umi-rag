"""Carga de configuración y resolución de rutas del chunking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_config(config_path: Path) -> dict[str, Any]:
    """Carga un archivo de configuración JSON cuyo nivel superior es un objeto."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"No se ha encontrado el archivo de configuración: {config_path}"
        )
    if not config_path.is_file():
        raise ValueError(
            f"La ruta de configuración no corresponde a un archivo: {config_path}"
        )

    try:
        with config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"El archivo de configuración no contiene un JSON válido. "
            f"Error en la línea {error.lineno}, columna {error.colno}: {error.msg}"
        ) from error

    if not isinstance(config, dict):
        raise ValueError("La configuración debe ser un objeto JSON.")
    if "strategies" in config and not isinstance(config["strategies"], list):
        raise ValueError(
            "La configuración debe contener una lista llamada 'strategies'."
        )
    return config


def resolve_config_path(
    project_root: Path,
    value: Any,
    setting_name: str,
) -> Path:
    """Resuelve una ruta de configuración relativa a la raíz del proyecto."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{setting_name}' debe ser una ruta no vacía.")

    path = Path(value)
    return path if path.is_absolute() else project_root / path
