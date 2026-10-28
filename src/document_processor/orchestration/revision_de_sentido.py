from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from document_processor.orchestration.documento_entero import json_en_marco
from document_processor.schema import ClinicalJson

NUMERO_RE = re.compile(r"\d+(?:[.,]\d+)?")
_PARTE_DE_RUTA_RE = re.compile(r"[^.\[\]]+|\[\d{1,6}\]")
_VALOR_CERRADO_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_MIN_CITA = 20
_MAX_CITA_COMPARADA = 120
_MAX_TEXTO_ACTUAL_COMPARADO = 60
_MIN_PROPORCION_DE_LONGITUD = 0.6

TAREA_DE_REVISION = """Debajo tienes el JSON que se generó a partir del documento de arriba.
Busca SOLO los textos del JSON cuyo sentido difiere del documento: un cuantificador o una cautela
más débil o más fuerte, un alcance o una condición perdidos, una causa o un efecto atribuidos a
otro fármaco, grupo o estudio, o algo que el documento descarta escrito como si lo confirmara (o
al revés). No toques cifras, no borres nada, no añadas información nueva y no cambies la redacción
de lo que ya es fiel.
Devuelve JSON: {"correcciones": [{"ruta": "ruta del campo con puntos y corchetes",
"texto_actual": "...", "texto_corregido": "...",
"cita": "frase exacta del documento que lo justifica"}]}.
Si no hay nada que corregir, {"correcciones": []}."""


@dataclass(frozen=True)
class Correccion:
    ruta: str
    antes: str
    despues: str


def tarea(datos: ClinicalJson) -> str:
    visibles = {clave: valor for clave, valor in datos.items() if not clave.startswith("_")}
    return f"<json>\n{json_en_marco(visibles)}\n</json>\n\n{TAREA_DE_REVISION}"


def _plano(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip().lower()


def buscar_hueco(datos: Any, ruta: str) -> tuple[Any, Any] | None:
    padre: Any = None
    clave: Any = None
    nodo = datos
    for parte in _PARTE_DE_RUTA_RE.findall(ruta):
        padre = nodo
        if parte.startswith("["):
            clave = int(parte[1:-1])
            if not isinstance(nodo, list) or clave >= len(nodo):
                return None
        else:
            clave = parte
            if not isinstance(nodo, dict) or clave not in nodo:
                return None
        nodo = nodo[clave]
    return (padre, clave) if padre is not None else None


def _aceptable(actual: Any, previsto: Any, nuevo: Any, cita: Any, fuente: str) -> bool:
    return (
        isinstance(actual, str)
        and not _VALOR_CERRADO_RE.fullmatch(actual)
        and isinstance(previsto, str)
        and bool(previsto.strip())
        and _plano(previsto)[:_MAX_TEXTO_ACTUAL_COMPARADO] in _plano(actual)
        and isinstance(nuevo, str)
        and len(nuevo.strip()) >= _MIN_PROPORCION_DE_LONGITUD * len(actual.strip())
        and sorted(NUMERO_RE.findall(actual)) == sorted(NUMERO_RE.findall(nuevo))
        and isinstance(cita, str)
        and len(cita) >= _MIN_CITA
        and _plano(cita)[:_MAX_CITA_COMPARADA] in fuente
    )


def aplicar(datos: ClinicalJson, respuesta: str, md: str) -> list[Correccion]:
    try:
        propuestas = json.loads(respuesta).get("correcciones") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("La revisión de sentido no devolvió un objeto JSON") from exc
    fuente = _plano(md)
    aplicadas: list[Correccion] = []
    for propuesta in propuestas if isinstance(propuestas, list) else []:
        if not isinstance(propuesta, dict):
            continue
        ruta = str(propuesta.get("ruta", ""))
        hueco = None if ruta.startswith("_") else buscar_hueco(datos, ruta)
        if hueco is None:
            continue
        padre, clave = hueco
        nuevo = propuesta.get("texto_corregido")
        if _aceptable(
            padre[clave], propuesta.get("texto_actual"), nuevo, propuesta.get("cita"), fuente
        ):
            aplicadas.append(Correccion(ruta, padre[clave], str(nuevo)))
            padre[clave] = nuevo
    return aplicadas