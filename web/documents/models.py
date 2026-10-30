"""Núcleo documental: `SourceDocument` con su máquina de estados y notas.

El documento vive una sola vez; la biblioteca y el pipeline son vistas de este dato.
Ver docs/ARCHITECTURE.md §4.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords


class Status(models.TextChoices):
    UPLOADED = "UPLOADED", "Subido"
    PARSING = "PARSING", "Parseando"
    PARSED = "PARSED", "Texto extraído"
    JSON_READY = "JSON_READY", "JSON generado"
    INDEXING = "INDEXING", "Indexando"
    INDEXED = "INDEXED", "Indexado"
    SPLIT = "SPLIT", "Dividido en capítulos"
    FAILED = "FAILED", "Fallido"
    WITHDRAWN = "WITHDRAWN", "Retirado"


class DocType(models.TextChoices):
    PAPER = "paper", "Paper"
    FICHA_TECNICA = "ficha_tecnica", "Ficha técnica"
    GUIA_CLINICA = "guia_clinica", "Guía clínica"


class Stage(models.TextChoices):
    PARSE = "parse", "Parseo"
    JSON = "json", "Generación JSON"
    INDEX = "index", "Indexado"


ERROR_MSG_LIMIT = 500

_REINDEX_FROM_INDEXED: set[str] = {Status.INDEXING}
_RETRY_FROM_FAILED: set[str] = {Status.PARSING, Status.JSON_READY, Status.INDEXING}
_YA_PROCESADO: frozenset[str] = frozenset(
    {Status.JSON_READY, Status.INDEXING, Status.INDEXED, Status.SPLIT}
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    Status.UPLOADED: {Status.PARSING},
    Status.PARSING: {Status.PARSED, Status.FAILED, Status.SPLIT},
    Status.PARSED: {Status.JSON_READY, Status.FAILED},
    Status.JSON_READY: {Status.INDEXING, Status.FAILED, Status.UPLOADED, Status.WITHDRAWN},
    Status.INDEXING: {Status.INDEXED, Status.FAILED, Status.UPLOADED},
    Status.INDEXED: _REINDEX_FROM_INDEXED | {Status.UPLOADED, Status.WITHDRAWN},
    Status.SPLIT: {Status.UPLOADED},
    Status.FAILED: _RETRY_FROM_FAILED | {Status.WITHDRAWN},
    Status.WITHDRAWN: {Status.JSON_READY},
}


class InvalidTransition(ValidationError):
    """Transición de estado no permitida."""


class ConfigVersion(models.Model):
    """Huella inmutable de la configuración de indexado (procedencia, Fase 6).

    Identifica de forma determinista el conjunto (modelo de embeddings + parámetros
    de chunking + versión de esquema) con el que se generó un indexado. Se registra
    en cada `SourceDocument` al indexar para poder reconstruir el estado del corpus
    a posteriori (ver docs/ARCHITECTURE.md §10). Append-only: nunca se edita.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fingerprint = models.CharField(max_length=64, unique=True, editable=False)
    embedding_model = models.CharField(max_length=128)
    target_tokens = models.PositiveIntegerField()
    max_tokens = models.PositiveIntegerField()
    schema_version = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.embedding_model}/{self.schema_version}/{self.fingerprint[:8]}"

    @classmethod
    def get_active(cls) -> ConfigVersion:
        """Devuelve la configuración vigente de chunking e indexación."""

        import json
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]

        with (project_root / "configs/chunker_config.json").open(
            encoding="utf-8"
        ) as f:
            chunker_config = json.load(f)

        with (project_root / "configs/indexing_config.json").open(
            encoding="utf-8"
        ) as f:
            indexing_config = json.load(f)

        model = indexing_config["embeddings"]["model_name"]
        target_tokens = int(chunker_config["tokenizer"]["target_tokens"])
        max_tokens = int(chunker_config["tokenizer"]["max_tokens"])

        if not isinstance(model, str) or not model.strip():
            raise ValueError("No se ha configurado el modelo de embeddings.")

        schema_version = "JC-1"

        raw = f"{model}|{target_tokens}|{max_tokens}|{schema_version}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        obj, _ = cls.objects.get_or_create(
            fingerprint=fingerprint,
            defaults={
                "embedding_model": model,
                "target_tokens": target_tokens,
                "max_tokens": max_tokens,
                "schema_version": schema_version,
            },
        )

        return obj



class SourceDocument(models.Model):
    """Un PDF y su ciclo de vida hasta el RAG."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sha256 = models.CharField(max_length=64, unique=True, help_text="Hash del PDF (dedup)")
    pdf_file = models.FileField(upload_to="pdfs/", null=True, blank=True)
    json_file = models.FileField(upload_to="json/", null=True, blank=True)
    # Markdown intermedio (PDF→MD por OpenDataLoader) que alimenta al LLM. Se persiste para poder
    # mostrarlo junto al PDF y al JSON en el detalle; solo los docs procesados tras añadir el campo.
    md_file = models.FileField(upload_to="md/", null=True, blank=True)
    tables_file = models.FileField(upload_to="tablas/", null=True, blank=True)
    original_filename = models.CharField(max_length=512, blank=True)

    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="chapters",
        help_text="Guía madre cuando este documento es un capítulo de una guía dividida.",
    )
    chapter_heading = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Encabezado del capítulo dentro de la guía madre; vacío salvo en capítulos.",
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)
    # Progreso de la generación por secciones (solo informativo, para el tracker en vivo): cuántas
    # secciones se han completado de las totales y el nombre de la última. Lo actualiza el worker
    # sección a sección mientras el documento está en PARSING.
    progress_done = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)
    progress_label = models.CharField(max_length=120, blank=True, default="")
    doc_type = models.CharField(max_length=20, choices=DocType.choices, blank=True, default="")
    llm_usage = models.JSONField(default=dict, db_default={}, blank=True, editable=False)

    title = models.CharField(max_length=512, blank=True)
    authors = models.JSONField(default=list, blank=True)
    journal = models.CharField(max_length=255, blank=True)
    year = models.CharField(max_length=8, blank=True)
    doi = models.CharField(max_length=128, blank=True)
    pmid = models.CharField(max_length=32, blank=True)
    atc_groups = models.JSONField(default=list, blank=True)
    active_ingredient = models.CharField(max_length=512, blank=True, default="")

    chunk_count = models.PositiveIntegerField(default=0)
    qdrant_ids = models.JSONField(default=list, blank=True)
    config_version = models.ForeignKey(
        "documents.ConfigVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text="Config con la que se indexó (procedencia, Fase 6).",
    )

    error_stage = models.CharField(max_length=10, choices=Stage.choices, blank=True, default="")
    error_msg = models.TextField(blank=True)

    collections = models.ManyToManyField("library.Collection", related_name="documents", blank=True)
    tags = models.ManyToManyField("library.Tag", related_name="documents", blank=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        """Nombre a mostrar: título → DOI → nombre de archivo → id."""
        return self.title or self.doi or self.original_filename or f"Documento {self.pk}"

    @property
    def extraccion_parcial(self) -> bool:
        """El JSON existe pero no representa el documento: faltan secciones o no pasó la revisión.

        Se deriva del estado en vez de guardarse en un campo propio para no exigir migración de
        esquema. Un estado `PARTIAL` sería más preciso y va en la PR que añada `QUARANTINED`.
        """
        return (
            bool(self.json_file) and self.status == Status.FAILED and self.error_stage == Stage.JSON
        )

    @property
    def ya_procesado(self) -> bool:
        """Ya convertido: con JSON (indexándose o indexado) o partido en capítulos (`SPLIT`).

        Es la guarda de idempotencia de `process_document` frente a las entregas duplicadas de la
        cola: un documento en estos estados no se reconvierte. `SPLIT` entra aquí porque una guía
        madre ya delegó su contenido en los capítulos; reprocesarla los duplicaría. El reintento
        legítimo llega en `FAILED`, que no entra aquí.
        """
        return self.status in _YA_PROCESADO

    def reiniciar_para_reproceso(self) -> None:
        """Devuelve un documento ya procesado al inicio del pipeline para regenerar su JSON.

        El reproceso reutiliza el camino normal (`process_document` desde `UPLOADED`); volver a
        `UPLOADED` es lo que distingue un reproceso pedido a mano de una entrega duplicada de la
        cola, que nunca reinicia y por eso la guarda de `ya_procesado` la sigue omitiendo.
        """
        self.transition_to(Status.UPLOADED)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(
        self,
        new_status: str,
        *,
        error_stage: str = "",
        error_msg: str = "",
        save: bool = True,
    ) -> None:
        """Cambia de estado validando la máquina de estados.

        Lanza `InvalidTransition` si la transición no está permitida.

        `error_msg` se recorta a `ERROR_MSG_LIMIT` aquí y no solo en quien llama, porque los
        mensajes de excepción del motor de ingesta pueden arrastrar texto del documento y
        este campo se replica en el historial de auditoría.
        """
        if not self.can_transition_to(new_status):
            raise InvalidTransition(f"Transición no permitida: {self.status} → {new_status}")
        self.status = new_status
        if new_status == Status.FAILED:
            self.error_stage = error_stage
            self.error_msg = error_msg[:ERROR_MSG_LIMIT]
        else:
            self.error_stage = ""
            self.error_msg = ""
        if save:
            self.save()


class MotivoRetirada(models.TextChoices):
    REVOCADO = "revocado", "Autorización revocada"
    SUSPENDIDO = "suspendido", "Autorización suspendida"
    NO_COMERCIALIZADO = "no_comercializado", "No comercializado"
    SIN_FICHA = "sin_ficha", "Sin ficha técnica segmentada"


class CimaRegistration(models.Model):
    """Número de registro de CIMA → documento que lo contiene, con el hash de su contenido.

    Existe porque **un mismo SmPC se publica bajo varios números de registro**: medido sobre 500
    fichas del registro, 71 de ellas (el 14,2%) son alias de otra, y con ellas se va el 20,8% de
    los caracteres. El caso extremo del censo es un SmPC de metotrexato compartido por seis
    concentraciones, y el más común, dos.

    El criterio de colapso es **identidad exacta del documento completo**, y es sin pérdida por
    construcción: solo une lo que es literalmente el mismo texto. No sirve el `sha256` del PDF, que
    es lo que ya guarda `SourceDocument`: los PDF de Montelukast Cinfa 4 mg y 5 mg pesan los mismos
    226.693 bytes y tienen hashes distintos, mientras sus 21 secciones son idénticas byte a byte.
    Y la propiedad de seguridad está medida, no supuesta: de los 44 grupos del censo, **ninguno**
    cruza laboratorio ni principio activo.

    Lo que distingue a los registros colapsados —concentración, nombre, forma, comercialización y
    códigos nacionales— no se descarta: va a `_source.aliases` del documento canónico, y sus
    códigos nacionales se suman a `national_codes` para que la financiación de BiFiMed cubra todas
    las presentaciones. Sin eso, colapsar sí perdería dato.

    El hash también es la clave de la resincronización periódica: reingerir un registro cuyo
    contenido no cambió no tiene que gastar ni un embedding.

    **La unicidad de la huella en la base es lo que hace correcta la desduplicación, no el orden de
    la cola**: la de `SourceDocument.sha256`, que para una ficha de CIMA es la huella, y detrás el
    índice único parcial sobre `content_hash`. El worker corre con `--pool=threads --concurrency=4`,
    así que cuatro alias del mismo SmPC pueden leer «no hay gemelo» a la vez —cada uno hace sus
    peticiones al registro antes de esa comprobación— y crear cuatro documentos con texto idéntico,
    que es justo el envenenamiento por casi-duplicados que esta tabla existe para evitar. Con la
    unicidad, el segundo en insertar choca, y quien pierde vuelve a leer y se cuelga como alias.
    """

    nregistro = models.CharField(primary_key=True, max_length=32)
    document = models.ForeignKey(
        SourceDocument, on_delete=models.CASCADE, related_name="cima_registrations"
    )
    content_hash = models.CharField(max_length=64, db_index=True)
    es_canonico = models.BooleanField(
        default=False,
        help_text="Registro que presenta el documento. Uno por contenido, garantizado por índice.",
    )
    national_codes = models.JSONField(default=list, blank=True)
    retirado_en = models.DateTimeField(null=True, blank=True)
    motivo_retirada = models.CharField(
        max_length=20, choices=MotivoRetirada.choices, blank=True, default=""
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nregistro"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_hash"],
                condition=models.Q(es_canonico=True),
                name="un_solo_documento_por_contenido",
            )
        ]

    def __str__(self) -> str:
        return f"CIMA {self.nregistro} → {self.document_id}"

    @property
    def activo(self) -> bool:
        return self.retirado_en is None


class EstadoSincronizacion(models.TextChoices):
    EN_CURSO = "en_curso", "En curso"
    TERMINADA = "terminada", "Terminada"
    CON_ERRORES = "con_errores", "Terminada con errores"
    FALLIDA = "fallida", "Fallida"


class CimaSyncRun(models.Model):
    carga_inicial = models.BooleanField(default=False)
    desde = models.DateField(null=True, blank=True)
    estado = models.CharField(
        max_length=12, choices=EstadoSincronizacion.choices, default=EstadoSincronizacion.EN_CURSO
    )
    iniciada_en = models.DateTimeField(auto_now_add=True)
    terminada_en = models.DateTimeField(null=True, blank=True)
    cambios_leidos = models.PositiveIntegerField(default=0)
    revisados = models.PositiveIntegerField(default=0)
    error_msg = models.TextField(blank=True)

    class Meta:
        ordering = ["-iniciada_en", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["estado"],
                condition=models.Q(estado="en_curso"),
                name="una_sola_sincronizacion_en_curso",
            )
        ]

    def __str__(self) -> str:
        iniciada = timezone.localtime(self.iniciada_en)
        return f"Sincronización CIMA {iniciada:%Y-%m-%d %H:%M} ({self.estado})"


class TipoEvento(models.TextChoices):
    ALTA = "alta", "Añadido"
    REACTIVADA = "reactivada", "Vuelve a estar disponible"
    RETIRADA = "retirada", "Retirado"
    ACTUALIZADA = "actualizada", "Ficha actualizada"
    ERROR = "error", "Error"


TIPOS_QUE_AVISAN = (TipoEvento.ALTA, TipoEvento.REACTIVADA, TipoEvento.RETIRADA)


class CimaSyncEvent(models.Model):
    run = models.ForeignKey(CimaSyncRun, on_delete=models.CASCADE, related_name="eventos")
    nregistro = models.CharField(max_length=32, db_index=True)
    nombre = models.CharField(max_length=512, blank=True)
    tipo = models.CharField(max_length=12, choices=TipoEvento.choices)
    document = models.ForeignKey(
        SourceDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="eventos_cima",
    )
    detalle = models.CharField(max_length=500, blank=True)
    resuelto = models.BooleanField(default=False)
    creado_en = models.DateTimeField(auto_now_add=True)
    visto_en = models.DateTimeField(null=True, blank=True)
    visto_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        ordering = ["-creado_en", "-pk"]
        indexes = [models.Index(fields=["tipo", "visto_en"], name="evento_cima_por_avisar")]

    def __str__(self) -> str:
        return f"CIMA {self.nregistro}: {self.tipo}"


class FidelityReport(models.Model):
    """Informe del revisor de fidelidad para una ejecución PDF→JSON (fase 2: solo informa).

    Vive fuera del JSON clínico a propósito —el plan lo fija: modelo propio consultable desde
    el dashboard, no dentro de lo que se archiva y se trocea— y guarda el bloque completo que
    estampó el motor más dos números desnormalizados para ordenar sin abrir el JSON. Cada
    reproceso crea una fila nueva: el informe describe una ejecución, no el documento, y al
    reindexar desde el JSON archivado no hay Markdown, así que lo que se consulta es esto.

    Sin umbrales ni veredictos: se miden en la fase 4 del plan y decidir es del llamante.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        SourceDocument, on_delete=models.CASCADE, related_name="fidelity_reports"
    )
    variant = models.CharField(max_length=20, choices=DocType.choices)
    report = models.JSONField(editable=False)
    global_coverage = models.FloatField()
    min_section_coverage = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"Fidelidad de {self.document_id} ({self.created_at:%Y-%m-%d})"

    @classmethod
    def from_engine(cls, document: SourceDocument, report: dict[str, Any]) -> FidelityReport:
        """Persiste el bloque `_fidelity` tal cual lo estampó el motor.

        Los dos números desnormalizados se derivan aquí y no los declara el informe: una copia
        dentro del JSON podría divergir de lo que se ordena en el dashboard.
        """
        coverage = report["coverage"]
        return cls.objects.create(
            document=document,
            variant=report["variant"],
            report=report,
            global_coverage=coverage["global_coverage"],
            min_section_coverage=min(s["coverage"] for s in coverage["sections"].values()),
        )

    @property
    def uncovered_chars(self) -> int:
        """Caracteres que no llegaron a ninguna sección antes del LLM (capa 1: un hecho sin umbral).

        Se lee del informe en vez de desnormalizarse como `global_coverage`: el dashboard no ordena
        por él —sigue ordenando por la peor sección—, solo lo enseña para que la pérdida de entrada
        deje de estar enterrada en un pie de página. `> 0` es binario y no un corte calibrado, así
        que la fase 2 lo puede marcar sin invadir los umbrales que el plan reserva a la fase 4.
        """
        cobertura = self.report.get("coverage") or {}
        return int(cobertura.get("uncovered_chars", 0))

    @property
    def uncovered_ratio(self) -> float:
        """Fracción sin cubrir, para dar magnitud: 340.000 caracteres sueltos no dicen si son el
        68% de una guía enorme o el resto de una pequeña."""
        doc_chars = int(self.report.get("doc_chars", 0))
        return round(self.uncovered_chars / doc_chars, 4) if doc_chars else 0.0


class DocumentNote(models.Model):
    """Nota/anotación sobre un documento (estilo Zotero)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(SourceDocument, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Nota de {self.document_id}"


class CorpusSnapshot(models.Model):
    """Foto append-only del estado del corpus en una fecha (trazabilidad, §10).

    Guarda un `manifest` (documentos + sha256 + chunk_count + qdrant_ids +
    colecciones), la `ConfigVersion` vigente, un `corpus_hash` de integridad y un
    `backup_ref` que apunta al backup real de esa fecha. Es **inmutable**: una vez
    creado no se puede editar (ver `save`). Permite reconstruir qué documentos y qué
    config tenía el sistema en un momento dado.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=128, help_text="Etiqueta legible (vN / fecha).")
    manifest = models.JSONField(default=list, editable=False)
    corpus_hash = models.CharField(max_length=64, editable=False)
    backup_ref = models.CharField(
        max_length=512, blank=True, help_text="Puntero al backup del corpus de esa fecha."
    )
    config_version = models.ForeignKey(
        "documents.ConfigVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="snapshots",
    )
    documents = models.ManyToManyField(SourceDocument, related_name="snapshots", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.label or f"Snapshot {self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Append-only: se puede crear pero nunca modificar."""
        if not self._state.adding:
            raise InvalidTransition("CorpusSnapshot es inmutable: no se puede editar.")
        super().save(*args, **kwargs)

    @property
    def document_count(self) -> int:
        return len(self.manifest or [])
