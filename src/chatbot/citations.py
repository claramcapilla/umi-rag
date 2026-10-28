
# ============================================================
# GESTIÓN DE REFERENCIAS DOCUMENTALES DEL CHATBOT
# ============================================================

"""
Este módulo prepara las referencias documentales asociadas
a los chunks recuperados por el sistema RAG.

Responsabilidades:

1. Recibir los chunks recuperados por el retriever.
2. Extraer sus metadatos de procedencia.
3. Construir referencias documentales estructuradas.
4. Eliminar referencias duplicadas cuando corresponda.
5. Devolver las fuentes utilizadas para generar la respuesta.

Este módulo no consulta Qdrant, no construye prompts,
no genera respuestas y no almacena conversaciones.
"""


# ============================================================
# IMPORTS
# ============================================================

from typing import Any, Dict, List, Optional


# ============================================================
# EXTRAER REFERENCIA DE UN CHUNK
# ============================================================

def extract_citation(
    chunk: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Extrae la información de procedencia de un chunk.

    Parameters
    ----------
    chunk:
        Chunk recuperado por el retriever, con su texto
        y los metadatos almacenados en Qdrant.

    Returns
    -------
    Optional[Dict[str, Any]]
        Referencia documental estructurada.

        Devuelve None si el chunk no contiene
        información suficiente para identificar
        el documento de origen.
    """

    # --------------------------------------------------------
    # 1. Obtener identificadores del documento
    # --------------------------------------------------------

    document_id = chunk.get("document_id")
    source_file = chunk.get("source_file")
    source_path = chunk.get("source_path")

    if not document_id and not source_file and not source_path:
        return None

    # --------------------------------------------------------
    # 2. Construir referencia documental
    # --------------------------------------------------------

    citation = {
        "document_id": document_id,
        "document_type": chunk.get("document_type"),
        "source_file": source_file,
        "source_path": source_path,
        "chunk_index": chunk.get("chunk_index"),
        "original_chunk_id": chunk.get(
            "original_chunk_id"
        ),
    }

    return citation


# ============================================================
# EXTRAER REFERENCIAS DE LOS CHUNKS
# ============================================================

def extract_citations(
    chunks: List[Dict[str, Any]],
    unique_documents: bool = True,
) -> List[Dict[str, Any]]:
    """
    Extrae las referencias documentales de los chunks.

    Parameters
    ----------
    chunks:
        Lista de chunks recuperados por el retriever.

    unique_documents:
        Si es True, devuelve una única referencia
        por documento.

        Si es False, conserva una referencia
        por cada chunk recuperado.

    Returns
    -------
    List[Dict[str, Any]]
        Lista de referencias documentales.
    """

    citations = []
    seen = set()

    # --------------------------------------------------------
    # 1. Recorrer chunks recuperados
    # --------------------------------------------------------

    for chunk in chunks:

        citation = extract_citation(chunk)

        if citation is None:
            continue

        # ----------------------------------------------------
        # 2. Determinar identificador de referencia
        # ----------------------------------------------------

        if unique_documents:
            citation_key = (
                citation["source_path"]
                or citation["document_id"]
                or citation["source_file"]
            )
        else:
            citation_key = (
                citation["original_chunk_id"]
                or (
                    citation["source_path"],
                    citation["chunk_index"],
                )
            )

        # ----------------------------------------------------
        # 3. Evitar duplicados
        # ----------------------------------------------------

        if citation_key in seen:
            continue

        seen.add(citation_key)

        # ----------------------------------------------------
        # 4. Añadir referencia
        # ----------------------------------------------------

        citations.append(citation)

    return citations
