"""Vistas de la app documental: biblioteca, colecciones, subida, detalle y pipeline."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.files.uploadedfile import UploadedFile
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, Lower, NullIf
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin

from document_processor.extraction import FichaTecnicaNoIngerible
from documents.models import (
    TIPOS_QUE_AVISAN,
    CimaRegistration,
    CimaSyncEvent,
    CimaSyncRun,
    CorpusSnapshot,
    DocType,
    FidelityReport,
    SourceDocument,
    Stage,
    Status,
    TipoEvento,
)
from documents.naming import clean_name
from documents.reconciliation import ReconRow, reconciliation_by_specialty
from documents.services import (
    PdfRejected,
    ingest_folder,
    ingest_into,
    ingest_into_subspecialties,
    ingest_specialty_tree,
    reject_batch_if_too_large,
)
from library.models import Collection, Tag

logger = logging.getLogger(__name__)


def library(request: HttpRequest) -> HttpResponse:
    status = request.GET.get("status", "")
    tag_id = request.GET.get("tag", "")
    doc_type = request.GET.get("doc_type", "")
    collection_id = request.GET.get("collection", "")

    active_collection = (
        Collection.objects.filter(pk=collection_id).first() if collection_id else None
    )

    # Especialidades (raíces) con el recuento de TODO su subárbol: los documentos cuelgan de la
    # colección hoja (la subespecialidad), así que contar solo los directos daba 0 en la raíz.
    # `distinct=True` en cada `Count` evita que el join con las hijas multiplique el recuento
    # de documentos directos (y viceversa). Los capítulos de una guía partida no cuentan: heredan
    # las colecciones de la madre, así que sin filtrarlos inflarían el subárbol.
    sin_capitulos = Q(documents__parent__isnull=True)
    hijas_sin_capitulos = Q(children__documents__parent__isnull=True)
    specialties = list(
        Collection.objects.filter(parent__isnull=True, is_specialty_root=True)
        .annotate(
            subtree_count=Count("documents", filter=sin_capitulos, distinct=True)
            + Count("children__documents", filter=hijas_sin_capitulos, distinct=True)
        )
        .order_by("name")
        .prefetch_related("children")
    )

    status_metrics: list[dict[str, object]] = []
    type_metrics: list[dict[str, object]] = []
    if active_collection is not None:
        base = SourceDocument.objects.filter(collections=active_collection, parent__isnull=True)
        con_json = base.exclude(json_file="").exclude(json_file__isnull=True).count()

        status_metrics = [
            {"label": "PDF", "n": base.count(), "color": "var(--st-up)"},
            {"label": "JSON", "n": con_json, "color": "var(--st-json)"},
            {
                "label": "Fallido",
                "n": base.filter(status=Status.FAILED).count(),
                "color": "var(--st-fail)",
            },
        ]
        type_counts = dict(base.values_list("doc_type").annotate(n=Count("id")))
        type_metrics = [
            {"label": "Paper", "value": DocType.PAPER, "color": "var(--st-json)"},
            {"label": "Ficha técnica", "value": DocType.FICHA_TECNICA, "color": "var(--st-run)"},
            {"label": "Guía clínica", "value": DocType.GUIA_CLINICA, "color": "var(--st-idx)"},
        ]
        for m in type_metrics:
            m["n"] = type_counts.get(m["value"], 0)
        docs = base
        if status:
            docs = docs.filter(status=status)
        if tag_id:
            docs = docs.filter(tags__id=tag_id)
        if doc_type:
            docs = docs.filter(doc_type=doc_type)
        docs = docs.order_by(Lower(Coalesce(NullIf("title", Value("")), "original_filename")))
        children = active_collection.children.annotate(
            doc_count=Count("documents", filter=Q(documents__parent__isnull=True))
        )
    else:
        docs = SourceDocument.objects.none()
        children = Collection.objects.none().annotate(doc_count=Count("documents"))

    uploaded_ids = [i for i in request.GET.get("uploaded", "").split(",") if i]
    batch = list(SourceDocument.objects.filter(pk__in=uploaded_ids)) if uploaded_ids else []

    prompt_html = (
        _render_markdown(active_collection.system_prompt)
        if active_collection and active_collection.system_prompt
        else None
    )
    norms_pretty = (
        json.dumps(active_collection.norms, ensure_ascii=False, indent=2)
        if active_collection and active_collection.norms
        else None
    )

    context = {
        "docs": docs,
        "children": children,
        "statuses": Status.choices,
        "active_status": status,
        "active_doc_type": doc_type,
        "type_metrics": type_metrics,
        "tags": Tag.objects.all() if active_collection is not None else Tag.objects.none(),
        "active_tag": tag_id,
        "active_collection": active_collection,
        "specialties": specialties,
        "status_metrics": status_metrics,
        "batch": batch,
        "prompt_html": prompt_html,
        "norms_pretty": norms_pretty,
        "descartados": request.session.pop("upload_descartados", []),
    }
    return render(request, "ui/library.html", context)


def _dashboard_state() -> dict[str, Any]:
    """Estado del tracker de conversión: filas por especialidad + totales + ritmo/ETA.

    Compartido por la página (`dashboard`) y el endpoint de refresco en vivo
    (`dashboard_data`) para no duplicar el cálculo.

    La reconciliación —un barrido de todos los documentos— se memoiza unos segundos
    (`DASHBOARD_CACHE_SECONDS`), porque con el polling se recalculaba en cada petición de cada
    pestaña. El resto (ritmo, ETA y el avance por sección de lo que está en proceso) se calcula
    fresco en cada llamada: es barato y es lo que el tracker enseña en vivo.
    """
    rows = cast(
        "list[ReconRow]",
        cache.get_or_set(
            "dashboard:reconciliation",
            reconciliation_by_specialty,
            settings.DASHBOARD_CACHE_SECONDS,
        ),
    )
    totals = {
        "total": sum(r["total"] for r in rows),
        "done": sum(r["done"] for r in rows),
        "failed": sum(r["failed"] for r in rows),
        "processing": sum(r["processing"] for r in rows),
        "pending": sum(r["pending"] for r in rows),
        "withdrawn": sum(r["withdrawn"] for r in rows),
    }
    totals["pct"] = round(100 * totals["done"] / totals["total"]) if totals["total"] else 0

    # Ritmo: documentos que alcanzaron JSON/indexado en la última hora (updated_at es
    # auto_now, así que el último cambio de estado lo marca). Proxy suficiente para un
    # ETA orientativo mientras el pipeline trabaja; sin conversiones recientes, sin ETA.
    hace_una_hora = timezone.now() - timedelta(hours=1)
    per_hour = SourceDocument.objects.filter(
        status__in=[Status.JSON_READY, Status.INDEXED, Status.SPLIT],
        updated_at__gte=hace_una_hora,
        parent__isnull=True,
    ).count()
    # Los FAILED son terminales: no son trabajo pendiente, así que no cuentan para el ETA. Sin
    # descontarlos, un corpus con documentos que fallan siempre (PDF ilegible, sin OCR) nunca
    # llega a done == total y el ETA marca «faltan N h» para siempre.
    remaining = totals["total"] - totals["done"] - totals["failed"]
    eta_hours = round(remaining / per_hour, 1) if per_hour and remaining > 0 else None

    # Documentos procesándose ahora, con su avance por secciones para el panel "En proceso".
    processing_states = [Status.PARSING, Status.PARSED, Status.INDEXING]
    processing_docs = [
        {
            "id": str(d.pk),
            "title": str(d),
            "done": d.progress_done,
            "total": d.progress_total,
            "label": d.progress_label,
            "pct": round(100 * d.progress_done / d.progress_total) if d.progress_total else 0,
        }
        for d in SourceDocument.objects.filter(
            status__in=processing_states, parent__isnull=True
        ).order_by("-updated_at")[:25]
    ]

    return {
        "rows": rows,
        "totals": totals,
        "per_hour": per_hour,
        "eta_hours": eta_hours,
        "processing_docs": processing_docs,
        "updated_at": timezone.localtime().strftime("%H:%M:%S"),
    }


def dashboard(request: HttpRequest) -> HttpResponse:
    """Tracker de conversión del corpus: avance por especialidad, en vivo."""
    return render(request, "ui/dashboard.html", {**_dashboard_state(), **_avisos_de_cima()})


def _avisos_de_cima() -> dict[str, Any]:
    from pipeline.cima_sync import documentos_por_retirar, resumen_de

    pendientes = CimaSyncEvent.objects.filter(tipo__in=TIPOS_QUE_AVISAN, visto_en__isnull=True)
    ultima = CimaSyncRun.objects.first()
    return {
        "avisos_cima": pendientes.select_related("document")[:MAX_AVISOS_CIMA],
        "avisos_cima_total": pendientes.count(),
        "ultima_sincronizacion": ultima,
        "resumen_sincronizacion": resumen_de(ultima) if ultima else [],
        "fichas_por_retirar": documentos_por_retirar().count(),
        "errores_cima": CimaSyncEvent.objects.filter(tipo=TipoEvento.ERROR, resuelto=False).count(),
    }


def dashboard_data(request: HttpRequest) -> JsonResponse:
    """Estado del tracker en JSON, para el refresco en vivo (polling) de la página."""
    return JsonResponse(_dashboard_state())


def fidelity(request: HttpRequest) -> HttpResponse:
    """Informes del revisor de fidelidad, el peor primero (fase 2: informa, no bloquea).

    Se ordena por la cobertura mínima de sección y no por la global: en la pérdida que motivó
    el plan la global era del 100% —otras secciones recibían el documento entero— mientras la
    sección que perdía contenido recibía el 14%. Ordenar por la global habría dejado ese
    documento al final de la lista.

    Solo el último informe de cada documento: los anteriores describen ejecuciones ya
    reemplazadas y enterrarían el último bajo el historial de un documento reprocesado. Se
    resuelve con subconsulta correlacionada y no con `distinct("document_id")`, que es
    DISTINCT ON y solo existe en PostgreSQL: la app corre sobre SQLite fuera del despliegue.
    """
    ultimo_del_documento = Subquery(
        FidelityReport.objects.filter(document_id=OuterRef("document_id"))
        .order_by("-created_at", "-pk")
        .values("pk")[:1]
    )
    informes = (
        FidelityReport.objects.filter(pk=ultimo_del_documento)
        .select_related("document")
        .order_by("min_section_coverage")
    )
    return render(request, "ui/fidelity.html", {"informes": informes})


def timeline(request: HttpRequest) -> HttpResponse:
    """Línea temporal: lista de snapshots del corpus (inmutables)."""
    snapshots = CorpusSnapshot.objects.all()
    return render(request, "ui/timeline.html", {"snapshots": snapshots})


def create_snapshot_view(request: HttpRequest) -> HttpResponse:
    """Crea un snapshot inmutable del estado actual del corpus."""
    if request.method == "POST":
        from pipeline.snapshots import create_snapshot

        create_snapshot(
            label=request.POST.get("label", "").strip(),
            backup_ref=request.POST.get("backup_ref", "").strip(),
        )
    return redirect("ui:timeline")


def snapshot_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Estado del sistema capturado en un snapshot (manifiesto + config)."""
    snapshot = get_object_or_404(CorpusSnapshot, pk=pk)
    return render(request, "ui/snapshot_detail.html", {"snap": snapshot})


def snapshot_compare(request: HttpRequest) -> HttpResponse:
    """Compara dos snapshots: altas, bajas, cambios y cambio de config."""
    older = get_object_or_404(CorpusSnapshot, pk=request.GET.get("a", ""))
    newer = get_object_or_404(CorpusSnapshot, pk=request.GET.get("b", ""))
    if older.created_at > newer.created_at:
        older, newer = newer, older

    from pipeline.snapshots import compare_snapshots

    diff = compare_snapshots(older, newer)
    return render(
        request,
        "ui/snapshot_compare.html",
        {"older": older, "newer": newer, "diff": diff},
    )


def _reenqueue(doc: SourceDocument) -> bool:
    """Reencola un documento fallido según la etapa en la que falló, si tiene sentido reintentarlo.

    Un PDF de ficha técnica **no se reintenta**: ese camino se retiró y volver a convertirlo es
    arrancar la JVM y la cascada de reparación de PDFs para acabar en la misma excepción. Sin esta
    guarda, cada clic en «reintentar fallidos» los reconvertía todos, el recuento de fallidos no
    podía bajar nunca, y nada distinguía esas filas de un fallo transitorio de verdad. Se reconoce
    por el tipo de excepción, que es el clasificador que `error_summary` deja al principio del
    mensaje.
    """
    if doc.error_msg.startswith(FichaTecnicaNoIngerible.__name__):
        return False
    if doc.error_stage == Stage.INDEX:
        from pipeline.index import index_document_task

        index_document_task.delay(str(doc.pk))
    else:
        from pipeline.tasks import process_document_task

        process_document_task.delay(str(doc.pk))
    return True


def retry_failed(request: HttpRequest) -> HttpResponse:
    """Reencola los documentos en FAILED (todos, o los de una especialidad)."""
    if request.method == "POST":
        from pipeline.index import resolve_specialties

        specialty = request.POST.get("specialty", "")
        failed = SourceDocument.objects.filter(status=Status.FAILED).prefetch_related("collections")
        omitidos = 0
        for doc in failed:
            if specialty and specialty not in resolve_specialties(doc):
                continue
            if not _reenqueue(doc):
                omitidos += 1
        if omitidos:
            messages.info(
                request,
                f"{omitidos} PDF de ficha técnica no se reintentan: esa vía se retiró, se ingieren "
                "desde CIMA por número de registro.",
            )
    return redirect("ui:dashboard")

@xframe_options_sameorigin
def serve_pdf(request: HttpRequest, pk) -> HttpResponse:
    """Sirve el PDF del documento (independiente de DEBUG/servidor de media)."""
    doc = get_object_or_404(SourceDocument, pk=pk)
    if not doc.pdf_file:
        raise Http404("El documento no tiene PDF.")
    resp = FileResponse(doc.pdf_file.open("rb"), content_type="application/pdf")
    resp["Cache-Control"] = "private, no-store"
    return resp


def serve_json(request: HttpRequest, pk: str) -> FileResponse:
    """Sirve el JSON clínico generado.

    Antes se enlazaba por `json_file.url`, que solo resolvía porque `config/urls.py`
    servía `MEDIA_ROOT` cuando `DEBUG` estaba activo: eso dejaba el JSON clínico
    accesible sin pasar por ninguna vista.
    """
    doc = get_object_or_404(SourceDocument, pk=pk)
    if not doc.json_file:
        raise Http404("El documento no tiene JSON generado.")
    resp = FileResponse(
        doc.json_file.open("rb"),
        content_type="application/json",
        as_attachment=True,
        filename=f"{doc.sha256}.json",
    )
    resp["Cache-Control"] = "private, no-store"
    return resp


def serve_md(request: HttpRequest, pk: str) -> FileResponse:
    """Sirve el Markdown intermedio (crudo) del documento."""
    doc = get_object_or_404(SourceDocument, pk=pk)
    if not doc.md_file:
        raise Http404("El documento no tiene Markdown intermedio.")
    resp = FileResponse(
        doc.md_file.open("rb"),
        content_type="text/markdown; charset=utf-8",
        as_attachment=True,
        filename=f"{doc.sha256}.md",
    )
    resp["Cache-Control"] = "private, no-store"
    return resp


def add_tag(request: HttpRequest, pk: str) -> HttpResponse:
    """Crea (si hace falta) y asigna una etiqueta al documento."""
    doc = get_object_or_404(SourceDocument, pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            tag, _ = Tag.objects.get_or_create(name=name)
            doc.tags.add(tag)
    return redirect("ui:detail", pk=pk)


def remove_tag(request: HttpRequest, pk: str, tag_pk: str) -> HttpResponse:
    """Quita una etiqueta del documento (no borra la etiqueta global)."""
    doc = get_object_or_404(SourceDocument, pk=pk)
    if request.method == "POST":
        tag = doc.tags.filter(pk=tag_pk).first()
        if tag is not None:
            doc.tags.remove(tag)
    return redirect("ui:detail", pk=pk)


def create_collection(request: HttpRequest) -> HttpResponse:
    """Crea una especialidad (colección raíz). No requiere documentos previos."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            collection, _ = Collection.objects.get_or_create(
                name=name,
                parent=None,
                defaults={"is_specialty_root": True},
            )
            return redirect(f"{reverse('ui:library')}?collection={collection.pk}")
    return redirect("ui:library")


def _encolar_tras_la_subida(doc_ids: list[str], *, procesar: bool) -> None:
    """Encola el procesamiento únicamente cuando se solicita."""

    if not procesar:
        return

    from pipeline.tasks import process_document_task

    for doc_id in doc_ids:
        process_document_task.delay(doc_id)


def _especialidad_destino(request: HttpRequest) -> Collection | None:
    """Especialidad bajo la que colgar una subespecialidad: una existente o una nueva.

    Devuelve `None` si no se puede determinar, y entonces la subida no ocurre: colgar una
    subespecialidad de la raíz la convertiría en especialidad, que no es lo que se pidió.
    """
    elegida = request.POST.get("parent_specialty", "").strip()
    if elegida:
        return Collection.objects.filter(pk=elegida, parent__isnull=True).first()
    nueva = clean_name(request.POST.get("new_specialty_name", "").strip())
    if not nueva:
        return None
    especialidad, _ = Collection.objects.get_or_create(
        name=nueva, parent=None, defaults={"is_specialty_root": True}
    )
    return especialidad


def _resolver_subida(
    request: HttpRequest,
    *,
    modo: str,
    files: list[UploadedFile],
    parent: Collection | None,
    collection_id: str,
) -> tuple[Collection | None, list[SourceDocument], list[str]] | None:
    """Reparte la subida según `modo` y devuelve `(destino, documentos, descartados)`.

    Devuelve `None` cuando no se puede determinar el destino —una subespecialidad sin
    especialidad, o «toda la especialidad» sobre un id que no es raíz—: el llamante redirige
    entonces sin subir nada. Extraído de `upload` para no pasar el límite de complejidad.
    """
    if modo == "especialidad":
        return ingest_specialty_tree(
            request.POST.get("specialty_name", "").strip(),
            files,
            request.POST.getlist("rel_paths"),
        )
    if modo == "especialidad_todo":
        especialidad = Collection.objects.filter(pk=collection_id, parent__isnull=True).first()
        if especialidad is None:
            return None
        docs, descartados = ingest_into_subspecialties(especialidad, files)
        return especialidad, docs, descartados
    if modo == "subespecialidad":
        especialidad = _especialidad_destino(request)
        if especialidad is None:
            return None
        return ingest_folder(especialidad, request.POST.get("subspecialty", "").strip(), files)
    docs, descartados = ingest_into(files, parent)
    return parent, docs, descartados


def upload(request: HttpRequest) -> HttpResponse:
    """Sube PDFs según lo que declare `modo`, que lo elige quien sube y no se adivina.

    - `especialidad`: la carpeta soltada es una especialidad y **cada subcarpeta suya es una
      subespecialidad**, que se crea. Los PDFs de su raíz cuelgan de la especialidad. El nombre
      se teclea y no se hereda de la carpeta: la del corpus se llama literalmente `PDF`.
    - `subespecialidad`: la carpeta soltada es una subespecialidad y hay que decir de qué
      especialidad cuelga, existente o nueva.
    - `especialidad_todo`: PDFs sueltos que interesan a la especialidad entera; se asocian a
      **todas** sus subespecialidades (opción «Toda la especialidad» del subidor).
    - cualquier otro valor: PDFs sueltos a la colección activa, el comportamiento de siempre.

    Antes el modo se **infería** de si venía `subspecialty`, y con una carpeta de especialidad el
    cliente aplanaba todo en una única subespecialidad con el nombre de la carpeta raíz.

    ``process_now`` encola además la etapa PDF→JSON. **Va desmarcada por defecto**: una subida por
    carpeta son cientos de PDFs, y cada uno arranca una JVM y una tanda de llamadas al LLM por la
    misma cola `ingest`. Ese gasto se pide, no se hereda de haber subido ficheros.
    """
    collection_id = request.POST.get("collection", "")
    parent = Collection.objects.filter(pk=collection_id).first() if collection_id else None

    uploaded_ids: list[str] = []
    descartados: list[str] = []
    target_id = collection_id
    if request.method == "POST":
        modo = request.POST.get("modo", "")
        files = request.FILES.getlist("pdfs")
        try:
            reject_batch_if_too_large(files)
            resultado = _resolver_subida(
                request, modo=modo, files=files, parent=parent, collection_id=collection_id
            )
        except PdfRejected:
            return redirect(reverse("ui:library"))
        if resultado is None:
            return redirect(reverse("ui:library"))
        destino, docs, descartados = resultado

        uploaded_ids = [str(d.pk) for d in docs]
        if destino is not None:
            target_id = str(destino.pk)
        if descartados:
            logger.warning("upload: %d ficheros descartados: %s", len(descartados), descartados)
            request.session["upload_descartados"] = descartados
        _encolar_tras_la_subida(uploaded_ids, procesar=bool(request.POST.get("process_now")))

    params = []
    if target_id:
        params.append(f"collection={target_id}")
    if uploaded_ids:
        params.append("uploaded=" + ",".join(uploaded_ids))
    query = ("?" + "&".join(params)) if params else ""
    return redirect(f"{reverse('ui:library')}{query}")


def upload_progress(request: HttpRequest) -> JsonResponse:
    """Estado actual de un lote de documentos (para el log de progreso de subida)."""
    ids = [i for i in request.GET.get("ids", "").split(",") if i]
    docs = SourceDocument.objects.filter(pk__in=ids)
    terminal = {Status.INDEXED, Status.FAILED}
    data = [
        {
            "id": str(d.pk),
            "name": d.display_name,
            "status": d.status,
            "label": d.get_status_display(),
            "done": d.status in terminal,
        }
        for d in docs
    ]
    return JsonResponse({"docs": data})


def _leer_clinical_json(doc: SourceDocument) -> Any:
    """JSON clínico para verlo en la ficha, o `None` si no hay o no se puede leer.

    Best-effort como el resto de la ficha: un JSON ausente o ilegible no debe tumbar el detalle,
    solo dejar el visor sin contenido. Se relee del fichero (no se cachea) porque un reproceso lo
    reescribe en su sitio.
    """
    if not doc.json_file:
        return None
    try:
        with doc.json_file.open("rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - visor best-effort sobre un fichero arbitrario
        logger.debug("detail: no se pudo leer el JSON de %s: %s", doc.pk, exc)
        return None
    return data


def _pdf_oficial(datos: Any) -> str | None:
    fuente = datos.get("_source") if isinstance(datos, dict) else None
    url = fuente.get("pdf_url") if isinstance(fuente, dict) else None
    if not isinstance(url, str):
        return None
    try:
        candidata = urlsplit(url)
    except ValueError:
        return None
    oficial = urlsplit(settings.CIMA_BASE_URL)
    return url if candidata.scheme == "https" and candidata.netloc == oficial.netloc else None


_md_renderer: Any = None


def _render_markdown(text: str) -> str:
    """Markdown a HTML con mistune, escapando el HTML crudo (salida segura para inyectar).

    El parser se memoiza a nivel de módulo: construirlo por petición es caro.
    """
    global _md_renderer
    if _md_renderer is None:
        import mistune

        _md_renderer = mistune.create_markdown(
            escape=True, plugins=["table", "strikethrough", "url"]
        )
    html: str = _md_renderer(text)
    return html


def _clinical_md_html(doc: SourceDocument) -> str | None:
    """Markdown intermedio renderizado a HTML para la ficha, o `None` si no hay o no se puede leer.

    Como el resto de la ficha es best-effort: un MD ausente o ilegible deja la pestaña vacía, no
    tumba el detalle.
    """
    if not doc.md_file:
        return None
    try:
        with doc.md_file.open("rb") as fh:
            text = fh.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - visor best-effort sobre un fichero arbitrario
        logger.debug("detail: no se pudo leer el MD de %s: %s", doc.pk, exc)
        return None
    return _render_markdown(text)


def _back_collection(doc: SourceDocument) -> Collection | None:
    """La colección a la que «volver» desde la ficha: la especialidad/subespecialidad del doc.

    Antes la ficha volvía siempre al inicio (raíz de la biblioteca), perdiendo la especialidad
    desde la que se abrió el documento. Un documento se ingiere en su colección hoja (la
    subespecialidad), así que se prefiere esa; si por lo que sea solo cuelga de una raíz, se usa
    esa; y si no tiene ninguna, la ficha cae al inicio como antes.
    """
    cols = list(doc.collections.all())
    return next((c for c in cols if c.parent_id is not None), cols[0] if cols else None)


def document_detail(request: HttpRequest, pk: str) -> HttpResponse:
    doc = get_object_or_404(SourceDocument, pk=pk)
    datos = _leer_clinical_json(doc)
    clinical_json = None if datos is None else json.dumps(datos, ensure_ascii=False, indent=2)
    es_ficha_cima = doc.cima_registrations.exists()
    clinical_md = _clinical_md_html(doc)
    # Pestaña activa por defecto: la primera disponible en el orden PDF · MD · JSON.
    default_view = (
        "pdf" if doc.pdf_file else "md" if clinical_md else "json" if clinical_json else ""
    )
    return render(
        request,
        "ui/detail.html",
        {
            "doc": doc,
            "fidelidad": doc.fidelity_reports.first(),
            "clinical_json": clinical_json,
            "clinical_md": clinical_md,
            "default_view": default_view,
            "es_ficha_cima": es_ficha_cima,
            "pdf_oficial": _pdf_oficial(datos) if es_ficha_cima else None,
            "back_collection": _back_collection(doc),
            "chapters": doc.chapters.order_by("uploaded_at"),
        },
    )


def doc_progress(request: HttpRequest, pk: str) -> JsonResponse:
    """Estado y avance por secciones de un documento, para el refresco en vivo del detalle."""
    doc = get_object_or_404(SourceDocument, pk=pk)
    pct = round(100 * doc.progress_done / doc.progress_total) if doc.progress_total else 0
    return JsonResponse(
        {
            "status": doc.status,
            "status_display": doc.get_status_display(),
            "done": doc.progress_done,
            "total": doc.progress_total,
            "label": doc.progress_label,
            "pct": pct,
        }
    )


def process(request: HttpRequest, pk: str) -> HttpResponse:
    """Encola la conversión PDF → JSON del documento.

    Un documento ya procesado se regenera: se reinicia al inicio del pipeline y se reencola. La
    plantilla avisa antes con un `confirm`, porque regenerar reemplaza el JSON existente. La guarda
    de idempotencia de `process_document` no lo bloquea porque el reinicio lo saca de `ya_procesado`
    —una entrega duplicada, que no reinicia, sí la sigue omitiendo—.
    """
    if request.method == "POST":
        from pipeline.tasks import process_document_task

        doc = get_object_or_404(SourceDocument, pk=pk)
        if doc.cima_registrations.exists():
            messages.error(
                request,
                "Esta ficha técnica viene de CIMA y no tiene PDF: se actualiza desde CIMA.",
            )
            return redirect("ui:detail", pk=pk)
        if doc.chapters.filter(
            status__in=[Status.UPLOADED, Status.PARSING, Status.PARSED, Status.INDEXING]
        ).exists():
            messages.error(
                request, "Sus capítulos se están procesando: se regenera cuando terminen."
            )
            return redirect("ui:detail", pk=pk)
        if doc.ya_procesado:
            doc.reiniciar_para_reproceso()
            messages.info(request, "Regenerando el JSON del documento; se reemplaza el anterior.")

        process_document_task.delay(str(pk))
    return redirect("ui:detail", pk=pk)


def index(request: HttpRequest, pk: str) -> HttpResponse:
    """(Re)indexa el documento en Qdrant utilizando su JSON clínico.

    Un documento con extracción parcial no se encola siquiera. La plantilla ya no ofrece el botón
    y la tarea lo rechaza, pero un POST directo llegaría aquí, y encolar para que falle en el
    worker no le dice nada a quien lo pidió.
    """
    if request.method == "POST":
        from pipeline.index import index_document_task

        doc = get_object_or_404(SourceDocument, pk=pk)
        if doc.extraccion_parcial:
            logger.warning("index: rechazado, %s tiene un JSON incompleto o inválido", pk)
            return redirect("ui:detail", pk=pk)

        index_document_task.delay(str(pk))
    return redirect("ui:detail", pk=pk)


def enrich(request: HttpRequest, pk: str) -> HttpResponse:
    """Actualiza los metadatos bibliográficos (Crossref/PubMed) del documento."""
    if request.method == "POST":
        from documents.tasks import enrich_document_task

        enrich_document_task.delay(str(pk))
    return redirect("ui:detail", pk=pk)


_CRITERIOS_CIMA = {"practiv1": "principio activo", "nombre": "nombre del medicamento"}
MAX_FICHAS_POR_LOTE = 50
MAX_AVISOS_CIMA = 50
MAX_EJECUCIONES_CIMA = 20
MAX_EVENTOS_CIMA = 500


def _entero(valor: object) -> int | None:
    """Entero de un parámetro de URL o de una respuesta ajena, o `None` si no lo es.

    Devolver `None` y no un defecto es lo que permite distinguir «CIMA no declaró el total» de «el
    total es este». Con un defecto, `totalFilas` ausente caía a `len(candidatos)` y la interfaz
    afirmaba que 200 era el total de una consulta de 418, escondiendo 218 resultados **con un
    segundo número corroborando la mentira**: peor que la línea que decía «200 registros» sin
    afirmar que fuera el total, que es justo el bug que este cambio venía a cerrar. El resto del
    código de CIMA es fail-closed; este campo no puede ser la excepción.
    """
    try:
        return int(str(valor))
    except ValueError:
        return None


def _criterio_de_busqueda(request: HttpRequest) -> dict[str, str]:
    """La consulta con la que volver a la página, tomada del formulario que se envió.

    Incluye la **página**: sin ella, ingerir una selección hecha en la página 3 devolvía a la 1, con
    otros 200 registros distintos, así que no se veían las marcas de «ya ingerida» recién ganadas ni
    qué se había aceptado, y había que volver a navegar. Con `MAX_FICHAS_POR_LOTE` fichas contra una
    lista de 418, recorrer un principio activo grande pasa por aquí en cada lote. Va saneada, que
    viene de un formulario.
    """
    return {
        "q": request.POST.get("q", "").strip(),
        "por": request.POST.get("por", "practiv1"),
        "pagina": str(max(1, _entero(request.POST.get("pagina")) or 1)),
    }


def _candidatos_de_cima(resultados: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convierte la respuesta de búsqueda en candidatos, diciendo cuáles no son ingeribles.

    Se marca en vez de filtrarse: si alguien busca un medicamento y no aparece, lo que aprende es
    que «no está», cuando la verdad puede ser que CIMA no publique su ficha segmentada. Desde que
    se retiró el camino PDF+LLM esa ficha no tiene vía, y eso hay que verlo en la lista en vez de
    descubrirlo al fallar.

    Los códigos nacionales **no salen de aquí**: la respuesta de búsqueda no incluye
    `presentaciones`. Los pide el constructor al endpoint de detalle, y de eso depende que la
    financiación de un biológico no se quede sin sus indicaciones —45 filas en dupilumab, 36 en
    omalizumab—.
    """
    from document_processor.cima.docs import ficha_segmentada

    candidatos: list[dict[str, Any]] = []
    for medicamento in resultados:
        candidatos.append(
            {
                "nregistro": medicamento.get("nregistro"),
                "nombre": medicamento.get("nombre"),
                "laboratorio": medicamento.get("labtitular"),
                "generico": bool(medicamento.get("generico")),
                "comercializado": bool(medicamento.get("comerc")),
                "ingerible": ficha_segmentada(medicamento) is not None,
            }
        )
    return candidatos


def fichas(request: HttpRequest) -> HttpResponse:
    """Ventana de fichas técnicas: búsqueda local y navegación por grupo ATC.

    Busca **sobre lo ya ingerido**, no contra CIMA: es instantáneo, y tras el recorrido masivo
    están todas las comercializadas. Casa por nombre del medicamento, principio activo y número de
    registro —el principio activo va en su propio campo porque el título es la marca («DARZALEX…»),
    así que buscar «daratumumab» no lo encontraría por el título—. Para lo que aún no esté ingerido,
    la plantilla enlaza a la búsqueda en vivo de `/cima/`.

    Una tarjeta por documento: la huella ya colapsó los registros del mismo SmPC, así que dos
    concentraciones idénticas salen una vez y una forma distinta (IV vs. subcutánea) sale aparte,
    que es lo correcto. Se pagina porque son miles.

    Solo entran las fichas ingeridas desde CIMA —las que tienen `CimaRegistration`—, no cualquier
    documento con `doc_type` de ficha: un PDF de ficha subido queda `FAILED` con ese tipo y no es
    lo que esta ventana promete. El alcance se expresa con `Exists`, no con un join, para que ni el
    conteo de facetas ni el listado dupliquen por registro. Los contadores de cada grupo ATC se
    calculan sobre la búsqueda activa (no sobre el total) para que el número case con lo que abre el
    filtro, y con una sola lectura de `atc_groups` en vez de un `COUNT` por grupo.
    """
    from document_processor.cima.atc import ATC_GROUP_LABELS

    consulta = request.GET.get("q", "").strip()
    grupo = request.GET.get("atc", "")

    ingerida_de_cima = CimaRegistration.objects.filter(document=OuterRef("pk"))
    base = SourceDocument.objects.filter(doc_type=DocType.FICHA_TECNICA).filter(
        Exists(ingerida_de_cima)
    )

    buscada = base
    if consulta:
        coincide_registro = CimaRegistration.objects.filter(
            document=OuterRef("pk"), nregistro__icontains=consulta
        )
        buscada = buscada.filter(
            Q(title__icontains=consulta)
            | Q(active_ingredient__icontains=consulta)
            | Exists(coincide_registro)
        )

    conteos: Counter[str] = Counter()
    for grupos_doc in buscada.values_list("atc_groups", flat=True):
        conteos.update(set(grupos_doc or []))
    grupos = [
        {"code": code, "label": label, "n": conteos[code]}
        for code, label in ATC_GROUP_LABELS.items()
        if conteos[code]
    ]

    docs = buscada
    if grupo:
        docs = docs.filter(atc_groups__contains=[grupo])
    docs = docs.order_by(
        Lower(
            Coalesce(NullIf("title", Value("")), NullIf("active_ingredient", Value("")), "sha256")
        )
    ).prefetch_related("cima_registrations")

    paginator = Paginator(docs, 50)
    page = paginator.get_page(request.GET.get("page"))
    filas = [
        {
            "doc": doc,
            "grupos": [ATC_GROUP_LABELS[g] for g in doc.atc_groups if g in ATC_GROUP_LABELS],
            "registros": [r.nregistro for r in doc.cima_registrations.all()],
        }
        for doc in page
    ]

    return render(
        request,
        "ui/fichas.html",
        {
            "filas": filas,
            "page": page,
            "total": paginator.count,
            "grupos": grupos,
            "grupo_activo": grupo,
            "consulta": consulta,
        },
    )


def cima(request: HttpRequest) -> HttpResponse:
    """Busca fichas técnicas en el registro de la AEMPS y las ofrece para ingerir.

    La clave de CIMA es el número de registro, que nadie sabe de memoria, así que se busca por
    principio activo o por nombre. No se ofrece ingerir de golpe todo un principio activo: son 72
    registros de montelukast con un 92-98% de texto compartido, y meterlos todos es envenenar la
    recuperación con casi-idénticos. Se elige.

    **La paginación no es opcional**: medido, `practiv1=paracetamol` da 418 resultados y el registro
    sirve 200 por página, así que sin pedir `pagina` la interfaz mostraba 200 y escondía 218.

    El tamaño de página se lee de `tamanioPagina`, **que viene en la misma respuesta** que ya se
    destripa para `resultados` y `totalFilas`. Transcrito a mano como constante, la aritmética era
    correcta por coincidencia: el día que el registro sirviera otro tamaño —o uno distinto por
    criterio— el enlace de siguiente desaparecería antes de tiempo y volverían a quedar registros
    inalcanzables mientras la cabecera afirma que existen, que es exactamente el fallo que se
    arregla aquí. Leído de la respuesta, se autocorrige a lo que sirva el servidor.

    Si `tamanioPagina` no viniera, el defecto es lo que el servidor **sirvió** en esta página, no
    cero: negar la siguiente con un total de 418 delante volvería a esconder resultados. Cero se
    reserva para la página vacía, donde hace falta, porque `pagina * 0 < total` es cierto para
    cualquier total y ofrecería una página siguiente tras otra para siempre.
    """
    from document_processor.cima import CimaClient
    from document_processor.cima.errors import CimaUnavailable

    consulta = request.GET.get("q", "").strip()
    criterio = request.GET.get("por", "practiv1")
    if criterio not in _CRITERIOS_CIMA:
        criterio = "practiv1"
    pagina = max(1, _entero(request.GET.get("pagina")) or 1)

    candidatos: list[dict[str, Any]] = []
    error = ""
    total: int | None = None
    por_pagina = 0
    if consulta:
        try:
            datos = CimaClient.from_settings().buscar({criterio: consulta, "pagina": pagina})
        except CimaUnavailable as exc:
            logger.warning("Búsqueda en CIMA fallida: %s", exc)
            error = "CIMA no respondió a la búsqueda. Inténtalo de nuevo."
        else:
            candidatos = _candidatos_de_cima(datos.get("resultados") or [])
            por_pagina = _entero(datos.get("tamanioPagina")) or len(candidatos)
            total = _entero(datos.get("totalFilas"))
            if total is None:
                logger.warning(
                    "CIMA no declaró totalFilas para %s=%r: no se puede afirmar el total",
                    criterio,
                    consulta,
                )

    estados = dict(
        CimaRegistration.objects.filter(
            nregistro__in=[c["nregistro"] for c in candidatos]
        ).values_list("nregistro", "document__status")
    )
    for candidato in candidatos:
        estado = estados.get(str(candidato["nregistro"]))
        candidato["ingerido"] = estado is not None
        candidato["fallido"] = estado == Status.FAILED

    if por_pagina <= 0:
        hay_siguiente = False
    elif total is None:
        hay_siguiente = len(candidatos) >= por_pagina
    else:
        hay_siguiente = pagina * por_pagina < total

    return render(
        request,
        "ui/cima.html",
        {
            "consulta": consulta,
            "criterio": criterio,
            "criterios": _CRITERIOS_CIMA,
            "candidatos": candidatos,
            "error": error,
            "pagina": pagina,
            "total": total,
            "hay_siguiente": hay_siguiente,
            "especialidades": Collection.objects.filter(
                parent__isnull=True, is_specialty_root=True
            ).order_by("name"),
            "max_por_lote": MAX_FICHAS_POR_LOTE,
        },
    )


def cima_ingest(request: HttpRequest) -> HttpResponse:
    """Encola la ingesta de los registros marcados, en la cola única de ingesta.

    Tres guardas, y ninguna es decorativa. **La especialidad es obligatoria** porque el indexado
    exige que el documento esté en una colección raíz: sin ella la ficha gastaría todas sus
    peticiones al registro para morir en la última etapa. **El número de registro se valida** antes
    de encolar con la misma forma que admite la sincronización (`cima.NREGISTRO`): es la clave del
    registro y va en las peticiones a CIMA, y basura ahí es un fallo en el worker que el usuario
    no ve. Y **el lote se acota**, porque cada ficha son
    peticiones a la AEMPS en una cola única: un formulario con mil casillas bloquea la ingesta y
    convierte el despliegue en un amplificador contra la Administración.
    """
    if request.method != "POST":
        return redirect("ui:cima")

    from document_processor.cima import NREGISTRO
    from pipeline.tasks import ingest_ficha_task

    destino = f"{reverse('ui:cima')}?{urlencode(_criterio_de_busqueda(request))}"
    especialidad = Collection.especialidad_por_id(request.POST.get("especialidad", ""))
    if especialidad is None:
        messages.error(request, "Elige la especialidad bajo la que colgar las fichas.")
        return redirect(destino)

    nregistros = list(
        dict.fromkeys(n for n in request.POST.getlist("nregistro") if NREGISTRO.fullmatch(n))
    )
    if not nregistros:
        messages.error(request, "Ningún número de registro válido en la selección.")
        return redirect(destino)
    if len(nregistros) > MAX_FICHAS_POR_LOTE:
        messages.error(
            request,
            f"Máximo {MAX_FICHAS_POR_LOTE} fichas por lote: cada una son varias peticiones al "
            "registro y comparten la cola con el resto de la ingesta.",
        )
        return redirect(destino)

    for nregistro in nregistros:
        ingest_ficha_task.delay(nregistro, str(especialidad.pk))
    messages.success(
        request,
        f"{len(nregistros)} ficha(s) encoladas en «{especialidad.name}». Cada una son varias "
        "peticiones al registro, así que aparecerán en la biblioteca en orden.",
    )
    return redirect(destino)


def cima_sync(request: HttpRequest) -> HttpResponse:
    from pipeline.cima_sync import resumen_de

    ejecuciones = CimaSyncRun.objects.all()
    elegida = (
        get_object_or_404(CimaSyncRun, pk=_entero(request.GET.get("run")) or 0)
        if request.GET.get("run")
        else ejecuciones.first()
    )
    eventos = (
        elegida.eventos.select_related("document") if elegida else CimaSyncEvent.objects.none()
    )
    return render(
        request,
        "ui/cima_sync.html",
        {
            "ejecuciones": ejecuciones[:MAX_EJECUCIONES_CIMA],
            "elegida": elegida,
            "resumen": resumen_de(elegida) if elegida else [],
            "eventos": eventos[:MAX_EVENTOS_CIMA],
            "eventos_total": eventos.count(),
        },
    )


def cima_sync_visto(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        pendientes = CimaSyncEvent.objects.filter(tipo__in=TIPOS_QUE_AVISAN, visto_en__isnull=True)
        if "todos" not in request.POST:
            ids = [i for i in (_entero(v) for v in request.POST.getlist("evento")) if i]
            pendientes = pendientes.filter(pk__in=ids)
        vistos = pendientes.update(visto_en=timezone.now(), visto_por=request.user)
        messages.success(request, f"{vistos} aviso(s) de CIMA marcados como vistos.")
    return redirect("ui:dashboard")
