"""Limpieza de nombres de archivos y carpetas para mostrarlos como nombres legibles.

Se aplica al subir: nombre de carpeta → subespecialidad, y nombre de fichero → título
de respaldo del documento. Quita rutas, extensión, numeración de orden y separadores.
"""

from __future__ import annotations

import re

_NUM_PREFIX = re.compile(r"^\s*\d{1,3}[\s._)\-]+")
_SEPARATORS = re.compile(r"[_\-]+")
_WS = re.compile(r"\s+")


def clean_name(raw: str) -> str:
    """Devuelve un nombre legible a partir de un nombre de archivo/carpeta crudo.

    Ej.: ``"01_arritmias-cardiacas.pdf"`` → ``"Arritmias cardiacas"``.
    """
    name = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = _SEPARATORS.sub(" ", name)
    name = _NUM_PREFIX.sub("", name)
    name = _WS.sub(" ", name).strip()
    if name:
        name = name[0].upper() + name[1:]
    return name
