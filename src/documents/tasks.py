"""Tareas Celery de la app de documentos."""

from __future__ import annotations

from celery import shared_task

from .enrichment import enrich_document


@shared_task(name="documents.enrich_document")  # type: ignore[untyped-decorator]
def enrich_document_task(doc_id: str) -> None:
    enrich_document(doc_id)
