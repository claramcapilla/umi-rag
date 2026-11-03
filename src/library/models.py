"""Modelos de organización de la biblioteca: colecciones (anidables) y etiquetas.

Estilo Zotero: un documento puede estar en varias colecciones sin duplicarse
(la relación M2M vive en `documents.SourceDocument`).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

FICHA_BUCKET_NAME = "Fichas técnicas (CIMA)"


class Collection(models.Model):
    """Colección anidable. La especialidad médica es una colección raíz."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    is_specialty_root = models.BooleanField(default=False)
    system_prompt = models.TextField(blank=True, default="")
    norms = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "name"], name="unique_collection_name_per_parent"
            )
        ]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def especialidad_por_id(cls, valor: str) -> Collection | None:
        """La especialidad con ese identificador, o `None` si no la hay.

        Se exige **`is_specialty_root`**, no solo ser raíz, porque esa es la propiedad de la que
        depende quien consume esto: `pipeline.index.resolve_specialties` recorre las colecciones
        buscando esa marca, y `documents.reconciliation` cuenta por ella. Una raíz con la marca
        quitada —el admin la expone y el default del campo es `False`— pasaría una comprobación de
        `parent__isnull` y luego no resolvería ninguna especialidad, así que el documento se
        quedaría sin indexar. Validar el proxy en vez de la invariante es cómo se cuela ese fallo.

        El identificador se valida antes de consultar porque `pk` es un UUID y **filtrar por algo
        que no lo es lanza excepción**, no devuelve vacío: un formulario sin elegir manda `""`
        (`ValueError`), y un `None` o un entero llegados por la cola de Celery darían `TypeError` y
        `AttributeError`. Se comprueba aquí en vez de envolver la consulta en un `except`, que
        también se tragaría un fallo de base de datos.
        """
        try:
            identificador = uuid.UUID(valor)
        except (ValueError, TypeError, AttributeError):
            return None
        return cls.objects.filter(
            pk=identificador, parent__isnull=True, is_specialty_root=True
        ).first()

    @classmethod
    def ficha_bucket(cls) -> Collection:
        """La colección raíz donde viven todas las fichas técnicas de CIMA.

        Es una raíz **sin** `is_specialty_root`, así que queda fuera de la lista de especialidades
        (la biblioteca y la reconciliación filtran por esa marca) y las fichas se navegan en su
        propia ventana en vez de mezclarse con el corpus por especialidad. No lleva especialidad a
        propósito: son ~16.000 medicamentos que no se pueden repartir a mano, y la clasificación es
        por grupo ATC (ver `SourceDocument.atc_groups`).

        Se crea de forma perezosa la primera vez que se ingiere una ficha, igual que las
        especialidades se crean al subir a ellas: así no aparece en una base donde nadie ha
        ingerido fichas todavía, y no ensucia el recuento de colecciones de la biblioteca.
        """
        bucket, _ = cls.objects.get_or_create(
            name=FICHA_BUCKET_NAME,
            parent=None,
            defaults={"is_specialty_root": False},
        )
        return bucket

    @classmethod
    def destino_de_ficha(cls, valor: str) -> Collection | None:
        """La colección donde cuelga una ficha: una especialidad raíz o el bucket de fichas.

        La ingesta de CIMA acepta esas dos y solo esas dos: la especialidad elegida por el camino
        clásico o el bucket para la ventana de fichas. Todo lo demás —una subespecialidad, una raíz
        cualquiera sin marca, un id inválido— se rechaza igual que en `especialidad_por_id`, porque
        una ficha colgada de donde el indexado no resuelve especialidad acabaría sin indexar. El
        bucket es la excepción deliberada: es una raíz sin marca, pero identificada por su nombre.
        """
        try:
            identificador = uuid.UUID(valor)
        except (ValueError, TypeError, AttributeError):
            return None
        return cls.objects.filter(
            Q(pk=identificador, parent__isnull=True)
            & (Q(is_specialty_root=True) | Q(name=FICHA_BUCKET_NAME))
        ).first()


class Tag(models.Model):
    """Etiqueta transversal libre."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
