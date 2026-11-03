"""Servicios de dominio de documentos: subida con dedup y captura rápida de metadatos."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from src.library.models import Collection

from .models import SourceDocument
from .naming import clean_name
from .quick_metadata import extract_quick_metadata


class PdfRejected(ValueError):
    """El fichero no se acepta para ingesta: no es un PDF o excede el tamaño máximo."""


def reject_batch_if_too_large(files: Sequence[UploadedFile]) -> None:
    """Guarda del lote completo, no de cada fichero.

    El tope por fichero no acota la petición: Django admite hasta
    ``DATA_UPLOAD_MAX_NUMBER_FILES`` ficheros por POST, y la subida por carpeta empuja
    justo a mandar muchos de golpe. Cada uno se lee entero a memoria en el hilo de la
    petición, así que el límite que importa para el agotamiento de memoria es el del lote.
    """
    total = sum(f.size or 0 for f in files)
    if total > settings.MAX_PDF_BATCH_BYTES:
        raise PdfRejected(
            f"El lote ocupa {total} bytes y el máximo es {settings.MAX_PDF_BATCH_BYTES}"
        )


def _reject_if_unacceptable(f: File[Any]) -> None:
    """Guarda de entrada, antes de leer el fichero a memoria.

    El tamaño se comprueba con ``f.size`` (metadato de la subida) para no cargar en
    memoria un fichero que vamos a descartar.
    """
    if not (f.name or "").lower().endswith(".pdf"):
        raise PdfRejected(f"No es un PDF: {f.name!r}")
    size = f.size or 0
    if size > settings.MAX_PDF_UPLOAD_BYTES:
        raise PdfRejected(
            f"El PDF ocupa {size} bytes y el máximo es {settings.MAX_PDF_UPLOAD_BYTES}"
        )


@transaction.atomic
def ingest_pdf(
    f: File[Any], collections: Sequence[Collection] | None = None
) -> tuple[SourceDocument, bool]:
    """Registra un PDF (subido o leído de disco), opcionalmente en una o varias colecciones.

    Al subir captura de forma automática (sin LLM) el título (metadatos del PDF) y el
    DOI (texto de las primeras páginas); el pipeline completo los refina después.

    Devuelve ``(documento, creado)``. Si el mismo PDF (por SHA-256) ya existe, devuelve
    el existente con ``creado=False`` (dedup); en ese caso también se asocia a las
    colecciones indicadas.

    El fichero se guarda con nombre acotado por hash, porque los nombres originales
    pueden superar el ``max_length`` del ``FileField``; el nombre legible se conserva en
    ``original_filename``.

    Lanza ``PdfRejected`` si no es un PDF o si excede ``MAX_PDF_UPLOAD_BYTES``.
    """
    _reject_if_unacceptable(f)
    pdf_bytes = f.read()
    f.seek(0)
    checksum = hashlib.sha256(pdf_bytes).hexdigest()

    existing = SourceDocument.objects.filter(sha256=checksum).first()
    if existing is not None:
        if collections:
            existing.collections.add(*collections)
        return existing, False

    meta = extract_quick_metadata(pdf_bytes)
    doc = SourceDocument(
        sha256=checksum,
        original_filename=clean_name(f.name or ""),
        title=meta.get("title", ""),
        doi=meta.get("doi", ""),
        doc_type=meta.get("doc_type", ""),
    )
    doc.pdf_file.save(f"{checksum}.pdf", ContentFile(pdf_bytes), save=False)
    doc.save()
    if collections:
        doc.collections.add(*collections)
    return doc, True


def ingest_into(
    files: Sequence[UploadedFile], collection: Collection | None
) -> tuple[list[SourceDocument], list[str]]:
    """Sube los ficheros a una colección —o a ninguna— y devuelve ``(documentos, descartados)``.

    Los descartados salen **por nombre y no por recuento**: en una carpeta de cientos de PDFs
    el que excede el tope por fichero desaparecía sin dejar rastro, y quien la sube da por hecho
    que están todos. Un corpus al que le falta un documento sin que nadie lo sepa es peor que
    una subida que falla.
    """
    cols = [collection] if collection is not None else []
    return _ingest_all(files, cols)


def _ingest_all(
    files: Sequence[UploadedFile], collections: Sequence[Collection]
) -> tuple[list[SourceDocument], list[str]]:
    docs: list[SourceDocument] = []
    descartados: list[str] = []
    for f in files:
        try:
            doc, _created = ingest_pdf(f, collections=collections)
        except PdfRejected:
            descartados.append(f.name or "(sin nombre)")
            continue
        docs.append(doc)
    return docs, descartados


def ingest_into_subspecialties(
    specialty: Collection, files: Sequence[UploadedFile]
) -> tuple[list[SourceDocument], list[str]]:
    """Sube PDFs sueltos y los asocia a **todas** las subespecialidades de `specialty`.

    Es la opción «Toda la especialidad» del subidor: un documento que interesa a la
    especialidad entera queda accesible desde cada una de sus subespecialidades. Si la
    especialidad aún no tiene ninguna, el documento cuelga de ella misma para no quedar
    huérfano. Cada PDF se registra una sola vez (dedup por SHA-256) y se asocia por M2M.
    """
    hijas = list(specialty.children.all())
    destinos = hijas or [specialty]
    return _ingest_all(files, destinos)


def ingest_folder(
    parent: Collection, subspecialty_name: str, files: Sequence[UploadedFile]
) -> tuple[Collection, list[SourceDocument], list[str]]:
    """Crea (o reutiliza) una subespecialidad bajo ``parent`` y sube sus PDFs.

    El nombre de la subespecialidad se limpia (``clean_name``). Devuelve
    ``(subespecialidad, documentos, descartados)``.

    Lanza ``PdfRejected`` si el lote completo excede ``MAX_PDF_BATCH_BYTES``, antes de
    crear la subespecialidad.
    """
    reject_batch_if_too_large(files)
    sub = _get_or_create_collection(clean_name(subspecialty_name), parent=parent)
    docs, descartados = ingest_into(files, sub)
    return sub, docs, descartados


def _get_or_create_collection(name: str, *, parent: Collection | None) -> Collection:
    """Colección con nombre ya limpio, rechazando el nombre vacío.

    `clean_name` puede vaciar un nombre entero —una carpeta llamada `01.` se queda en nada—, y
    una colección sin nombre es indistinguible del resto en la biblioteca y en el selector.
    """
    if not name:
        raise PdfRejected("El nombre de la colección no puede quedar vacío")
    collection, _ = Collection.objects.get_or_create(
        name=name, parent=parent, defaults={"is_specialty_root": parent is None}
    )
    return collection


def _subfolder_of(rel_path: str) -> str:
    """Subespecialidad que le toca a un fichero por su ruta relativa, o `''` si va suelto.

    La ruta la compone el navegador a partir del nombre de las carpetas del usuario, así que
    **es entrada no confiable**: se descartan los tramos `.` y `..` antes de mirar nada, y el
    nombre pasa por `clean_name`, que además se queda con el último tramo de cualquier resto de
    ruta. Nunca toca el sistema de ficheros; solo nombra una colección.

    Se **aplana al primer nivel** a propósito: la biblioteca muestra especialidad › subespecialidad
    y nada más, así que todo lo que cuelgue de `Anafilaxia/` acaba en `Anafilaxia` por hondo que
    esté.
    """
    tramos = [t for t in rel_path.replace("\\", "/").split("/") if t not in ("", ".", "..")]
    return clean_name(tramos[0]) if len(tramos) > 1 else ""


def ingest_specialty_tree(
    specialty_name: str, files: Sequence[UploadedFile], rel_paths: Sequence[str]
) -> tuple[Collection, list[SourceDocument], list[str]]:
    """Sube una carpeta de especialidad entera: cada subcarpeta suya es una subespecialidad.

    `rel_paths` son las rutas de cada fichero **relativas a la carpeta soltada**, en el mismo
    orden que `files`, y las pone el cliente. Si vinieran de menos, los que falten se tratan como
    sueltos en vez de quedarse fuera del bucle: perder ficheros por un desajuste de longitudes
    sería el mismo fallo silencioso que esto viene a cerrar.

    Los PDFs de la raíz —sin subcarpeta— cuelgan de la propia especialidad.

    Devuelve ``(especialidad, documentos, descartados)``.
    """
    reject_batch_if_too_large(files)
    especialidad = _get_or_create_collection(clean_name(specialty_name), parent=None)

    rutas = list(rel_paths) + [""] * (len(files) - len(rel_paths))
    subespecialidades: dict[str, Collection] = {}
    docs: list[SourceDocument] = []
    descartados: list[str] = []
    for f, ruta in zip(files, rutas, strict=False):
        nombre = _subfolder_of(ruta)
        if nombre and nombre not in subespecialidades:
            subespecialidades[nombre] = _get_or_create_collection(nombre, parent=especialidad)
        destino = subespecialidades[nombre] if nombre else especialidad
        nuevos, fallidos = ingest_into([f], destino)
        docs.extend(nuevos)
        descartados.extend(fallidos)
    return especialidad, docs, descartados
