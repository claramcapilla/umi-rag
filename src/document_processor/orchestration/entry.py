"""Punto de entrada del motor de ingesta: bytes de un PDF → JSON clínico.

Es lo único que docsrepo llama del motor. La capa de lote del paquete original —CLI con argparse,
barras de `rich`, pools de hilos anidados sobre carpetas— no vive aquí: docsrepo hace el lote con
Celery y el dashboard de reconciliación.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from document_processor.extraction import ConversionError, clean_markdown, convert_pdf
from document_processor.extraction.redaction import redact_personal_data
from document_processor.extraction.tablas_en_rejilla import tablas_en_rejilla
from document_processor.llm_config import get_llm_table_grid, get_llm_whole_document
from document_processor.schema import ClinicalJson

from .clinical_formatter import Enrutado, generate_clinical_json_with_llm


def pdf_bytes_to_clinical_json(
    pdf_bytes: bytes, on_progress: Callable[[int, int, str], None] | None = None
) -> tuple[ClinicalJson, dict[str, Any] | None]:
    """Convierte bytes de un PDF a JSON clínico, sin exigir rutas en disco al llamante.

    Devuelve el JSON y, aparte, el informe de fidelidad (`_fidelity`, capas 0 y 1): el informe
    va a un modelo propio consultable desde el dashboard, no dentro del JSON clínico, para no
    ensuciar lo que se archiva y se trocea. Separarlo aquí —y no en el llamante— es lo que hace
    imposible que un olvido lo deje dentro del fichero archivado.

    Descarta `_usage` (contabilidad de tokens, que ya va al log) pero **conserva `_extraction`**:
    ese bloque dice qué secciones no se pudieron generar, y quien decide si el documento sirve es
    el llamante. Quitarlo aquí sería volver a dar por completo un documento parcial.

    Esta función es el seam entre las dos etapas del pipeline, así que es donde se distinguen: todo
    lo que falle antes del LLM sale como `ConversionError` para que el llamante pueda etiquetarlo
    como parseo. Sin esa distinción, un PDF que no llega a Markdown —corrupto, escaneado sin capa
    de texto, JVM ausente— quedaba registrado en la etapa `JSON`, que significa otra cosa: que hay
    un JSON incompleto en disco que no debe indexarse.
    """
    md_text = pdf_bytes_to_markdown(pdf_bytes)
    return md_to_clinical_json(
        md_text,
        on_progress=on_progress,
        tablas_en_rejilla=pdf_bytes_to_tablas_en_rejilla(pdf_bytes),
    )


def pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    """Convierte los bytes de un PDF a Markdown limpio, o lanza `ConversionError`.

    Es la primera de las dos etapas del pipeline, expuesta aparte porque el llamante necesita el
    Markdown —y su tamaño— antes de decidir si una guía enorme se parte en capítulos o se genera de
    una pieza. Todo lo que falle aquí sale como `ConversionError` para que se etiquete como parseo:
    un PDF corrupto, escaneado sin capa de texto o sin JVM no llega a Markdown y no deja JSON.
    """
    return _desde_pdf_temporal(pdf_bytes, lambda ruta: clean_markdown(convert_pdf(ruta).markdown))


def pdf_bytes_to_tablas_en_rejilla(pdf_bytes: bytes) -> str:
    if not (get_llm_table_grid() and get_llm_whole_document()):
        return ""
    return _desde_pdf_temporal(pdf_bytes, tablas_en_rejilla)


def _desde_pdf_temporal(pdf_bytes: bytes, convertir: Callable[[Path], str]) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(pdf_bytes)
        tmp_path = Path(fh.name)
    try:
        return convertir(tmp_path)
    except Exception as exc:
        raise ConversionError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def md_to_clinical_json(
    md_text: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    tablas_en_rejilla: str = "",
    enrutado: Enrutado | None = None,
) -> tuple[ClinicalJson, dict[str, Any] | None]:
    """Segunda etapa: Markdown → JSON clínico por el LLM, con el informe de fidelidad aparte.

    Separada de la conversión para poder alimentarla con un capítulo ya troceado, sin PDF: es el
    camino de los documentos hijos de una guía partida. El Markdown viaja de vuelta como la clave
    privada `_markdown` —igual que `_extraction`—, para que el llamante lo archive aparte
    (`md_file`) y lo saque del JSON antes de guardarlo. `_usage` se descarta (ya va al log) y
    `_fidelity` se separa para no viajar dentro de lo que se archiva y se trocea.
    """
    md_text = redact_personal_data(md_text)
    clinical_json = generate_clinical_json_with_llm(
        md_text=md_text,
        parallel=True,
        on_progress=on_progress,
        tablas_en_rejilla=tablas_en_rejilla,
        enrutado=enrutado,
    )
    clinical_json.pop("_usage", None)
    fidelity: dict[str, Any] | None = clinical_json.pop("_fidelity", None)
    clinical_json["_markdown"] = md_text
    return clinical_json, fidelity