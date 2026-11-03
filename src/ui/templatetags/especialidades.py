"""Iconos por especialidad para la biblioteca.

El icono se deriva del nombre de la especialidad por coincidencia de palabra clave,
sin migración ni campo nuevo: es pura presentación y se aplica a las especialidades ya
existentes. Lo desconocido cae al estetoscopio genérico.
"""

from __future__ import annotations

import unicodedata

from django import template

register = template.Library()

_ICONO_POR_DEFECTO = "🩺"

_ICONOS: tuple[tuple[str, str], ...] = (
    ("cardio", "❤️"),
    ("neumo", "🫁"),
    ("neuro", "🧠"),
    ("psiqui", "💭"),
    ("dermato", "🩹"),
    ("piel", "🩹"),
    ("pediatr", "🧒"),
    ("gineco", "🤰"),
    ("obstetr", "🤰"),
    ("matern", "🤰"),
    ("traumato", "🦴"),
    ("ortoped", "🦴"),
    ("oftalmo", "👁️"),
    ("otorrino", "👂"),
    ("nefro", "🫘"),
    ("urolog", "💧"),
    ("gastro", "🍽️"),
    ("digest", "🍽️"),
    ("endocrin", "🦋"),
    ("tiroid", "🦋"),
    ("hematolo", "🩸"),
    ("oncolo", "🎗️"),
    ("odonto", "🦷"),
    ("dental", "🦷"),
    ("estomatolo", "🦷"),
    ("alerg", "🤧"),
    ("inmuno", "🤧"),
    ("infeccio", "🦠"),
    ("microbio", "🦠"),
    ("urgencia", "🚑"),
    ("emergencia", "🚑"),
    ("anestesi", "💉"),
    ("radiolo", "🩻"),
    ("imagen", "🩻"),
    ("rehabilita", "🧘"),
    ("fisioterap", "🧘"),
    ("geriatr", "👵"),
    ("nutric", "🥗"),
    ("dietetic", "🥗"),
    ("farmac", "💊"),
    ("cirug", "🔪"),
    ("quirurg", "🔪"),
)


def _normalizar(nombre: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", nombre)
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return sin_tildes.casefold()


@register.filter
def especialidad_icono(nombre: str) -> str:
    """Emoji para la especialidad `nombre`, o el estetoscopio si no se reconoce."""
    normalizado = _normalizar(nombre)
    for clave, icono in _ICONOS:
        if clave in normalizado:
            return icono
    return _ICONO_POR_DEFECTO
