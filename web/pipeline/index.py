
"""Conexión entre los documentos de Django y Qdrant."""

from __future__ import annotations

import json
import logging

from celery import shared_task
from django.db import transaction

from documents.models import SourceDocument, Stage, Status
from ingestion.index_document import index_clinical_document
from .errors import error_summary

logger = logging.getLogger(__name__)


def _normalize_specialty(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def resolve_specialties(doc: SourceDocument) -> list[str]:
    """Obtiene las especialidades raíz de las colecciones del documento."""

    specialties = []

    for collection in doc.collections.all():
        node = collection

        while node is not None:
            if node.is_specialty_root:
                specialty = _normalize_specialty(node.name)

                if specialty not in specialties:
                    specialties.append(specialty)

                break

            node = node.parent

    return specialties


def index_document(doc_id: str) -> None:
    """Indexa el JSON clínico de un documento utilizando umi-rag."""

    doc = SourceDocument.objects.get(pk=doc_id)

    if doc.status == Status.WITHDRAWN:
        return

    if doc.extraccion_parcial:
        logger.warning(
            "No se indexa %s porque su extracción está incompleta",
            doc.pk,
        )
        return

    if doc.status not in (Status.JSON_READY, Status.INDEXED):
        logger.warning(
            "No se puede indexar %s desde el estado %s",
            doc.pk,
            doc.status,
        )
        return

    try:
        doc.transition_to(Status.INDEXING)

        with doc.json_file.open("rb") as file:
            clinical_json = json.load(file)

        specialties = resolve_specialties(doc)

        if not specialties:
            raise ValueError(
                "El documento no está asignado a ninguna especialidad."
            )

        document_types = {
            "paper": "paper",
            "guia_clinica": "guideline",
            "ficha_tecnica": "ficha_tecnica",
        }

        document_type = document_types[doc.doc_type]

        result = index_clinical_document(
            clinical_json=clinical_json,
            document_id=str(doc.pk),
            document_type=document_type,
            source_file=doc.json_file.name,
            previous_ids=list(doc.qdrant_ids or []),
            specialties=specialties,
        )

        doc.qdrant_ids = result["qdrant_ids"]
        doc.chunk_count = result["chunk_count"]
        doc.transition_to(Status.INDEXED)

        logger.info(
            "Documento %s indexado: %s chunks",
            doc.pk,
            doc.chunk_count,
        )

    except Exception as exc:
        logger.exception("Error indexando el documento %s", doc.pk)
        doc.transition_to(
            Status.FAILED,
            error_stage=Stage.INDEX,
            error_msg=error_summary(exc),
        )


@shared_task(name="pipeline.index_document")
def index_document_task(doc_id: str) -> None:
    index_document(doc_id)

def retirar_documento(doc_id: str) -> None:
    """Retira de Qdrant una ficha sin registros CIMA vigentes."""

    from indexing.pipeline import load_config
    from indexing.qdrant_store import create_client, delete_points

    doc = SourceDocument.objects.get(pk=doc_id)

    if doc.status == Status.WITHDRAWN:
        return

    # Una ficha con registros vigentes no debe retirarse.
    if doc.cima_registrations.filter(retirado_en__isnull=True).exists():
        return

    if not doc.can_transition_to(Status.WITHDRAWN):
        raise ValueError(
            f"No se puede retirar {doc.pk} desde el estado {doc.status}"
        )

    config = load_config()
    qdrant_config = config["qdrant"]
    client = create_client(qdrant_config)

    try:
        delete_points(
            client,
            qdrant_config["collection_name"],
            list(doc.qdrant_ids or []),
        )
    finally:
        client.close()

    doc.qdrant_ids = []
    doc.chunk_count = 0
    doc.transition_to(Status.WITHDRAWN, save=False)
    doc.save(
        update_fields=[
            "status",
            "error_stage",
            "error_msg",
            "qdrant_ids",
            "chunk_count",
            "updated_at",
        ]
    )


def borrar_de_qdrant(
    objetivos: list[tuple[list[str], str]],
) -> None:
    """Elimina puntos obsoletos sin borrar IDs todavía utilizados."""

    from indexing.pipeline import load_config
    from indexing.qdrant_store import create_client, delete_points

    ids_objetivo = {
        point_id
        for ids, _ in objetivos
        for point_id in ids
    }

    if not ids_objetivo:
        return

    # Proteger puntos que siguen vinculados a documentos vigentes.
    ids_vigentes = {
        point_id
        for doc in SourceDocument.objects.exclude(
            status=Status.WITHDRAWN
        )
        for point_id in (doc.qdrant_ids or [])
    }

    ids_borrar = list(ids_objetivo - ids_vigentes)

    if not ids_borrar:
        return

    config = load_config()
    qdrant_config = config["qdrant"]
    client = create_client(qdrant_config)

    try:
        delete_points(
            client,
            qdrant_config["collection_name"],
            ids_borrar,
        )
    finally:
        client.close()

@shared_task(name="pipeline.borrar_de_qdrant")
def borrar_de_qdrant_task(
    objetivos: list[tuple[list[str], str]],
) -> None:
    borrar_de_qdrant(objetivos)

@shared_task(name="pipeline.retirar_documento")
def retirar_documento_task(doc_id: str) -> None:
    retirar_documento(doc_id)