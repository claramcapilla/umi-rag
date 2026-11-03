"""Reconciliación del corpus: conteos por especialidad (subidos/JSON/indexados/fallidos).

La especialidad de un documento son sus raíces `is_specialty_root` (ver
`src.pipeline.index.resolve_specialties`); un documento multi-especialidad cuenta en
cada una, como en el indexado. Los que no tienen ninguna raíz van a "Sin especialidad".
"""

from __future__ import annotations

from typing import TypedDict

from documents.models import SourceDocument, Status
from library.models import Collection
from pipeline.index import _normalize_specialty, resolve_specialties

NO_SPECIALTY = "Sin especialidad"


class ReconRow(TypedDict):
    """Fila de reconciliación de una especialidad.

    ``key`` es el nombre normalizado que usa el reintento dirigido; vacío significa
    documentos sin especialidad.
    """

    specialty: str
    key: str
    total: int
    json: int
    indexed: int
    failed: int
    # Partición del progreso por ESTADO (excluyentes entre sí, suman `total`): a diferencia de
    # `json`/`indexed`/`failed` —que son dimensiones acumulativas y solapables (un JSON bueno que
    # falló al indexar cuenta en `json` y en `failed`)—, estos cuatro clasifican cada documento en
    # exactamente un cubo según su estado actual, para un tracker de avance sin contradicciones.
    done: int
    processing: int
    pending: int
    withdrawn: int
    pct: int


_DONE_STATES = {Status.JSON_READY, Status.INDEXED, Status.SPLIT}
_PROCESSING_STATES = {Status.PARSING, Status.PARSED, Status.INDEXING}


def _tally(row: ReconRow, doc: SourceDocument, has_json: bool) -> None:
    """Suma un documento a su fila: las dimensiones acumulativas (solapables) y la partición
    de progreso por estado (excluyente, cada documento cae en un solo cubo)."""
    if doc.status == Status.WITHDRAWN:
        row["withdrawn"] += 1
        return
    row["total"] += 1
    if has_json:
        row["json"] += 1
    if doc.status == Status.INDEXED:
        row["indexed"] += 1
    if doc.status == Status.FAILED:
        row["failed"] += 1
    if doc.status in _DONE_STATES:
        row["done"] += 1
    elif doc.status in _PROCESSING_STATES:
        row["processing"] += 1
    elif doc.status == Status.UPLOADED:
        row["pending"] += 1


def reconciliation_by_specialty() -> list[ReconRow]:
    """Filas de reconciliación, una por especialidad (+ fila "Sin especialidad").

    Un documento multi-especialidad cuenta en cada una; los que no tienen ninguna
    raíz de especialidad van a "Sin especialidad".

    Los capítulos de una guía partida (`parent` no nulo) no se cuentan: una guía es un documento,
    no tantos como capítulos tenga. La guía madre, en estado `SPLIT`, cuenta como convertida —su
    contenido vive ya en los capítulos—, así que `SPLIT` entra en los estados «done».
    """
    display_by_key = {
        _normalize_specialty(c.name): c.name
        for c in Collection.objects.filter(is_specialty_root=True)
    }

    buckets: dict[str, ReconRow] = {}

    def bucket(key: str, display: str) -> ReconRow:
        return buckets.setdefault(
            key,
            ReconRow(
                specialty=display,
                key=key,
                total=0,
                json=0,
                indexed=0,
                failed=0,
                done=0,
                processing=0,
                pending=0,
                withdrawn=0,
                pct=0,
            ),
        )

    for doc in SourceDocument.objects.filter(parent__isnull=True).prefetch_related("collections"):
        specialties = resolve_specialties(doc)
        has_json = bool(doc.json_file) and not doc.extraccion_parcial
        targets = [(k, display_by_key.get(k, k)) for k in specialties] or [("", NO_SPECIALTY)]
        for key, display in targets:
            _tally(bucket(key, display), doc, has_json)

    rows = list(buckets.values())
    # El porcentaje mide avance de conversión = documentos en estado terminal con JSON
    # (JSON_READY o INDEXED) sobre el total. Un documento en reproceso vuelve a "processing",
    # así que la barra nunca marca 100% mientras algo sigue en curso.
    for row in rows:
        row["pct"] = round(100 * row["done"] / row["total"]) if row["total"] else 0
    rows.sort(key=lambda r: (r["key"] == "", r["specialty"].lower()))
    return rows
