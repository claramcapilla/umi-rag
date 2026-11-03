"""Enriquecimiento de metadatos bibliográficos de un documento (estilo Zotero).

Consulta Crossref/PubMed (``integration.bibliography``) por DOI o título y rellena
los campos bibliográficos del `SourceDocument` con datos autoritativos.
"""

from __future__ import annotations

from integration.bibliography import BibliographyClient

from .models import SourceDocument


def enrich_document(doc_id: str) -> bool:
    """Enriquece los metadatos de un documento. Devuelve ``True`` si hubo match.

    Autoritativo: los campos con valor devuelto sobrescriben al del documento; los
    que no se encuentren se dejan como están. Sin match (o sin red) → no cambia nada.

    **Guarda solo los campos bibliográficos, no la fila entera.** Con `process_now` en la subida
    las dos tareas entran juntas en la cola `ingest`: ésta tarda ~1 s en Crossref con el documento
    cargado de antes, y mientras `process_document` ya ha hecho `transition_to(PARSING)`. Un
    `save()` completo escribiría de vuelta el `status` que se leyó —`UPLOADED`—, dejando el
    documento como no procesado mientras un worker lo procesa. Con `update_fields` el `status` deja
    de estar en juego y el orden entre las dos tareas pasa a ser irrelevante.
    """
    doc = SourceDocument.objects.get(pk=doc_id)
    query_title = doc.title or doc.original_filename
    data = BibliographyClient.from_settings().enrich(doi=doc.doi, title=query_title)
    if not data:
        return False

    escritos: list[str] = []
    for field in ("title", "journal", "year", "doi", "pmid"):
        value = data.get(field)
        if value:
            setattr(doc, field, value)
            escritos.append(field)
    if data.get("authors"):
        doc.authors = data["authors"]
        escritos.append("authors")
    if not escritos:
        return False

    doc.save(update_fields=escritos)
    return True
