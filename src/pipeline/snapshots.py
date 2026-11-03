"""Snapshots del corpus: captura inmutable del estado documental en una fecha.

Un snapshot es **metadatos + puntero** (no una copia del dato): el `manifest`
lista los documentos con su hash, estado, chunks e IDs de Qdrant y colecciones; el
`backup_ref` apunta al backup real de esa fecha. Permite reconstruir qué había y con
qué `ConfigVersion` se procesó (ver docs/ARCHITECTURE.md §10).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict

from django.utils import timezone

from documents.models import ConfigVersion, CorpusSnapshot, SourceDocument


class ManifestEntry(TypedDict):
    """Una fila del manifiesto.

    `error_stage` acompaña a `status` porque `FAILED` solo no dice qué hay en disco: un fallo en
    etapa `INDEX` tiene un JSON clínico bueno que no llegó a Qdrant, y uno en etapa `JSON` tiene
    un JSON incompleto que no debe indexarse. Sin la etapa, el manifiesto registra los dos como el
    mismo documento, y esa diferencia es justo la que decide si el corpus puede reconstruirse
    desde el snapshot.

    Los manifiestos escritos antes de que existiera esta clave **no la llevan** y `CorpusSnapshot`
    es append-only, así que nadie va a rellenarla: quien lea entradas ajenas usa `.get`.
    """

    id: str
    sha256: str
    status: str
    error_stage: str
    doc_type: str
    chunk_count: int
    qdrant_ids: list[str]
    collections: list[str]
    config_version: str


def build_manifest() -> tuple[list[ManifestEntry], str]:
    """Construye el manifiesto del corpus actual y su hash de integridad.

    El hash es determinista (claves ordenadas) sobre el manifiesto, de modo que dos
    corpus idénticos producen el mismo `corpus_hash`.
    """
    entries: list[ManifestEntry] = []
    docs = SourceDocument.objects.prefetch_related("collections").order_by("sha256")
    for doc in docs:
        entries.append(
            ManifestEntry(
                id=str(doc.pk),
                sha256=doc.sha256,
                status=doc.status,
                error_stage=doc.error_stage,
                doc_type=doc.doc_type,
                chunk_count=doc.chunk_count,
                qdrant_ids=list(doc.qdrant_ids or []),
                collections=sorted(c.name for c in doc.collections.all()),
                config_version=(str(doc.config_version_id) if doc.config_version_id else ""),
            )
        )
    payload = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    corpus_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return entries, corpus_hash


def create_snapshot(*, label: str = "", backup_ref: str = "") -> CorpusSnapshot:
    """Crea un `CorpusSnapshot` inmutable del estado actual del corpus."""
    entries, corpus_hash = build_manifest()
    if not label:
        label = timezone.now().strftime("%Y-%m-%d %H:%M")
    snapshot = CorpusSnapshot.objects.create(
        label=label,
        manifest=entries,
        corpus_hash=corpus_hash,
        backup_ref=backup_ref,
        config_version=ConfigVersion.get_active(),
    )
    snapshot.documents.set(SourceDocument.objects.all())
    return snapshot


class SnapshotDiff(TypedDict):
    added: list[ManifestEntry]
    removed: list[ManifestEntry]
    changed: list[dict[str, Any]]
    config_changed: bool


def compare_snapshots(older: CorpusSnapshot, newer: CorpusSnapshot) -> SnapshotDiff:
    """Compara dos snapshots: altas, bajas y documentos con estado/etapa/chunks cambiados.

    La etapa entra en la comparación porque un documento puede moverse entre dos fallos distintos
    sin cambiar de `status` ni de `chunk_count`: de un fallo de indexado, con su JSON bueno, a una
    extracción parcial que no debe indexarse. Sin ella ese movimiento no aparecía en el diff.

    `error_stage` se lee con `.get`: los manifiestos anteriores a la clave no la tienen y el modelo
    es append-only, así que comparar un snapshot viejo contra uno nuevo —lo que ofrece la UI— es el
    caso normal, no el excepcional.
    """
    old_by_id = {e["id"]: e for e in older.manifest}
    new_by_id = {e["id"]: e for e in newer.manifest}

    added = [new_by_id[i] for i in new_by_id.keys() - old_by_id.keys()]
    removed = [old_by_id[i] for i in old_by_id.keys() - new_by_id.keys()]

    changed: list[dict[str, Any]] = []
    for doc_id in old_by_id.keys() & new_by_id.keys():
        before, after = old_by_id[doc_id], new_by_id[doc_id]
        if (
            before["status"] != after["status"]
            or before["chunk_count"] != after["chunk_count"]
            or before.get("error_stage", "") != after.get("error_stage", "")
        ):
            changed.append({"id": doc_id, "before": before, "after": after})

    return SnapshotDiff(
        added=sorted(added, key=lambda e: e["sha256"]),
        removed=sorted(removed, key=lambda e: e["sha256"]),
        changed=sorted(changed, key=lambda c: str(c["id"])),
        config_changed=older.config_version_id != newer.config_version_id,
    )
