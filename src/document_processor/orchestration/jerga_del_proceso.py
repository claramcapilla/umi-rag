from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

from document_processor.orchestration.documento_entero import json_en_marco
from document_processor.orchestration.revision_de_sentido import (
    NUMERO_RE,
    Correccion,
    buscar_hueco,
)

JERGA_RE = re.compile(
    r"\b(?:second|separate|alternative|secondary|supplementary|other|first|both|two|grid)[ -]"
    r"(?:pdf[ -])?(?:table[ -])?(?:grid[ -])?(?:detector[ -])?(?:readings?|renderings?)\b"
    r"|\btable[ -](?:grid|reading|detector)"
    r"|\b(?:segunda|primera|otra) lectura|\blas dos lecturas|\blectura de las? tablas?"
    r"|\botra (?:presentación|versión) de las? tablas?|\brejilla(?! de amsler)|\bdetector de tablas"
    r"|\b(?:supplied|provided|extracted|converted) (?:text|document|markdown|table)"
    r"|\btexto (?:proporcionado|suministrado|extraído|convertido)"
    r"|\bmarkdown\b|\bOCR[ -](?:artifacts?|artefacts?|errors?|corruption)"
    r"|\bconversion (?:artifacts?|artefacts?|errors?)|\berror(?:es)? de (?:conversión|extracción)",
    re.IGNORECASE,
)
_CELDA_DE_TABLA_RE = re.compile(r"\]\[\d+\]$")

TAREA_DE_JERGA = """Debajo tienes textos del JSON generado a partir del documento de arriba con
frases que hablan del proceso de extracción (lecturas de las tablas, rejilla, detector, texto
suministrado, conversión, Markdown, OCR) en vez de hablar del documento.
Para cada una de esas frases da la frase exacta tal como aparece en el texto y su versión
corregida, que habla solo del documento:
- Si dos versiones de una tabla discrepan, quédate con lo que da el documento y no menciones la
  discrepancia.
- Si la frase solo comenta el proceso o lo que falta en el texto, la versión corregida es "".
- No incluyas frases que en realidad hablan del documento, aunque usen las mismas palabras.
No añadas información ni cifras. El resto del texto no se toca.
Devuelve JSON: {"correcciones": [{"ruta": "la ruta tal como aparece abajo",
"frase_actual": "...", "frase_corregida": "..."}]}."""
_ESPACIOS_RE = re.compile(r"[ \t]{2,}")


def textos_con_jerga(nodo: Any, ruta: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if not clave.startswith("_"):
                yield from textos_con_jerga(valor, f"{ruta}.{clave}" if ruta else clave)
    elif isinstance(nodo, list):
        for indice, valor in enumerate(nodo):
            yield from textos_con_jerga(valor, f"{ruta}[{indice}]")
    elif isinstance(nodo, str) and JERGA_RE.search(nodo):
        yield ruta, nodo


def tarea(senaladas: dict[str, str]) -> str:
    return f"<textos>\n{json_en_marco(senaladas)}\n</textos>\n\n{TAREA_DE_JERGA}"


def _cifras(texto: str) -> Counter[str]:
    return Counter(NUMERO_RE.findall(texto))


def _aceptable(texto: str, frase: Any, corregida: Any, cifras_del_json: Counter[str]) -> bool:
    return (
        isinstance(frase, str)
        and isinstance(corregida, str)
        and bool(JERGA_RE.search(frase))
        and frase in texto
        and not JERGA_RE.search(corregida)
        and not _cifras(corregida) - _cifras(frase)
        and all(
            cifras_del_json[cifra] > veces
            for cifra, veces in (_cifras(frase) - _cifras(corregida)).items()
        )
    )


def _corregidos(
    respuesta: str, senaladas: dict[str, str], cifras_del_json: Counter[str]
) -> dict[str, str]:
    try:
        propuestas = json.loads(respuesta).get("correcciones") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("La corrección de la jerga no devolvió un objeto JSON") from exc
    corregidos: dict[str, str] = {}
    for propuesta in propuestas if isinstance(propuestas, list) else []:
        if (
            not isinstance(propuesta, dict)
            or (ruta := str(propuesta.get("ruta", ""))) not in senaladas
        ):
            continue
        texto = corregidos.get(ruta, senaladas[ruta])
        frase, corregida = propuesta.get("frase_actual"), propuesta.get("frase_corregida")
        if _aceptable(texto, frase, corregida, cifras_del_json):
            cifras_del_json.subtract(_cifras(str(frase)) - _cifras(str(corregida)))
            corregidos[ruta] = texto.replace(str(frase), str(corregida), 1)
    return {ruta: _ESPACIOS_RE.sub(" ", texto).strip() for ruta, texto in corregidos.items()}


def aplicar(datos: Any, respuesta: str, senaladas: dict[str, str]) -> list[Correccion]:
    aplicadas: list[Correccion] = []
    sobrantes: list[tuple[list[Any], int]] = []
    visibles = {c: v for c, v in datos.items() if not c.startswith("_")}
    cifras_del_json = _cifras(json.dumps(visibles, ensure_ascii=False))
    for ruta, nuevo in _corregidos(respuesta, senaladas, cifras_del_json).items():
        hallado = buscar_hueco(datos, ruta)
        if hallado is None or hallado[0][hallado[1]] != senaladas[ruta]:
            continue
        padre, clave = hallado
        aplicadas.append(Correccion(ruta, padre[clave], nuevo))
        if nuevo:
            padre[clave] = nuevo
        elif isinstance(padre, list) and not _CELDA_DE_TABLA_RE.search(ruta):
            sobrantes.append((padre, clave))
        else:
            padre[clave] = None
    for lista, indice in sorted(sobrantes, key=lambda par: par[1], reverse=True):
        del lista[indice]
    return aplicadas