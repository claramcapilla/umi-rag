"""Tests de la UI documental."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from documents.models import (
    CimaRegistration,
    CorpusSnapshot,
    DocType,
    FidelityReport,
    SourceDocument,
    Stage,
    Status,
)
from library.models import Collection, Tag


def _doc(sha: str, *, status: str = Status.UPLOADED, json: bool = False) -> SourceDocument:
    doc = SourceDocument(sha256=sha, status=status)
    doc.pdf_file.save(f"{sha}.pdf", SimpleUploadedFile("x.pdf", b"%PDF-1.4"), save=True)
    if json:
        doc.json_file.save(f"{sha}.json", SimpleUploadedFile("x.json", b"{}"), save=True)
    return doc


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evita que `.delay()` intente conectar al broker en tests (no hay Redis en CI).

    Los tests que verifican el encolado sobreescriben esto con su propio stub.
    """
    from documents.tasks import enrich_document_task
    from pipeline.index import index_document_task
    from pipeline.tasks import process_document_task

    for task in (enrich_document_task, process_document_task, index_document_task):
        monkeypatch.setattr(task, "delay", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _logged_in(db: None, client: Client, django_user_model: Any) -> None:
    """Toda la UI exige sesión y `is_staff` (`StaffRequiredMiddleware`).

    Los tests de esta batería comprueban comportamiento de las vistas, no el control de
    acceso; ese va aparte en los `test_requires_*`.
    """
    user = django_user_model.objects.create_user(username="editora", password="x", is_staff=True)
    client.force_login(user)


@pytest.mark.django_db
def test_login_page_stays_reachable_for_anonymous() -> None:
    """Si esto se rompe, el middleware deja a todo el mundo fuera sin forma de entrar."""
    anonymous = Client()

    assert anonymous.get(reverse("admin:login")).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["ui:library", "ui:dashboard", "ui:timeline"])
def test_requires_staff_rejects_authenticated_non_staff(
    url_name: str, django_user_model: Any
) -> None:
    curiosa = django_user_model.objects.create_user(username="curiosa", password="x")
    autenticada = Client()
    autenticada.force_login(curiosa)

    assert autenticada.get(reverse(url_name)).status_code == 403


@pytest.mark.django_db
def test_requires_staff_does_not_serve_documents_to_non_staff(django_user_model: Any) -> None:
    doc = _doc("4" * 64, json=True)
    curiosa = django_user_model.objects.create_user(username="curiosa", password="x")
    autenticada = Client()
    autenticada.force_login(curiosa)

    assert autenticada.get(reverse("ui:pdf", args=[doc.pk])).status_code == 403
    assert autenticada.get(reverse("ui:json", args=[doc.pk])).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    ["ui:library", "ui:dashboard", "ui:timeline", "ui:upload_progress"],
)
def test_requires_login_redirects_anonymous_get(url_name: str) -> None:
    anonymous = Client()

    resp = anonymous.get(reverse(url_name))

    assert resp.status_code == 302
    assert reverse("admin:login") in resp["Location"]


@pytest.mark.django_db
def test_requires_login_does_not_serve_pdf_to_anonymous() -> None:
    doc = _doc("5" * 64)
    anonymous = Client()

    resp = anonymous.get(reverse("ui:pdf", args=[doc.pk]))

    assert resp.status_code == 302
    assert reverse("admin:login") in resp["Location"]


@pytest.mark.django_db
def test_serve_json_returns_the_generated_json(client: Client) -> None:
    doc = _doc("7" * 64, json=True)

    resp = client.get(reverse("ui:json", args=[doc.pk]))

    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/json"
    assert resp["Cache-Control"] == "private, no-store"
    assert "attachment" in resp["Content-Disposition"]
    assert b"".join(resp.streaming_content) == b"{}"  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_upload_rejects_batch_over_the_limit(client: Client, settings: Any) -> None:
    settings.MAX_PDF_BATCH_BYTES = 40

    resp = client.post(
        reverse("ui:upload"),
        {
            "pdfs": [
                SimpleUploadedFile("a.pdf", b"%PDF-1.4 " + b"a" * 30),
                SimpleUploadedFile("b.pdf", b"%PDF-1.4 " + b"b" * 30),
            ]
        },
    )

    assert resp.status_code == 302
    assert SourceDocument.objects.count() == 0


@pytest.mark.django_db
def test_serve_json_404_without_json(client: Client) -> None:
    doc = _doc("8" * 64)

    assert client.get(reverse("ui:json", args=[doc.pk])).status_code == 404


@pytest.mark.django_db
def test_requires_login_does_not_serve_json_to_anonymous() -> None:
    doc = _doc("9" * 64, json=True)
    anonymous = Client()

    resp = anonymous.get(reverse("ui:json", args=[doc.pk]))

    assert resp.status_code == 302
    assert reverse("admin:login") in resp["Location"]


@pytest.mark.django_db
def test_requires_login_blocks_anonymous_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc("6" * 64)
    encolados: list[str] = []
    from pipeline.tasks import process_document_task

    monkeypatch.setattr(process_document_task, "delay", lambda doc_id: encolados.append(doc_id))
    anonymous = Client()

    for url in (
        reverse("ui:upload"),
        reverse("ui:process", args=[doc.pk]),
        reverse("ui:index", args=[doc.pk]),
        reverse("ui:retry_failed"),
    ):
        resp = anonymous.post(url)
        assert resp.status_code == 302, url
        assert reverse("admin:login") in resp["Location"], url

    assert encolados == []
    assert SourceDocument.objects.count() == 1


@pytest.mark.django_db
def test_library_renders(client: Client) -> None:
    resp = client.get(reverse("ui:library"))
    assert resp.status_code == 200
    assert b"Especialidades" in resp.content
    assert b"Subidos" not in resp.content


@pytest.mark.django_db
def test_los_capitulos_no_inflan_el_recuento_de_la_especialidad(client: Client) -> None:
    """1 guía = 1 documento: la subespecialidad cuenta la madre, no sus capítulos."""
    card = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    madre = _doc("1" * 64, status=Status.SPLIT)
    madre.collections.add(card)
    for i in range(3):
        capitulo = SourceDocument.objects.create(sha256=f"c{i}" + "0" * 62, parent=madre)
        capitulo.collections.add(card)

    resp = client.get(reverse("ui:library"))

    especialidades = {c.name: c for c in resp.context["specialties"]}
    assert especialidades["Cardiología"].subtree_count == 1


@pytest.mark.django_db
def test_el_detalle_de_una_guia_partida_enlaza_sus_capitulos(client: Client) -> None:
    madre = _doc("1" * 64, status=Status.SPLIT)
    SourceDocument.objects.create(sha256="c" * 64, parent=madre, chapter_heading="Capítulo Primero")

    resp = client.get(reverse("ui:detail", args=[madre.pk]))

    assert resp.status_code == 200
    assert b"Cap\xc3\xadtulo Primero" in resp.content


@pytest.mark.django_db
def test_upload_creates_document_and_redirects_with_batch(client: Client) -> None:
    f = SimpleUploadedFile("a.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = client.post(reverse("ui:upload"), {"pdfs": f})
    assert resp.status_code == 302
    assert SourceDocument.objects.count() == 1
    doc = SourceDocument.objects.get()
    assert f"uploaded={doc.pk}" in resp["Location"]


@pytest.mark.django_db
def test_upload_progress_json(client: Client) -> None:
    doc = SourceDocument(sha256="e" * 64, original_filename="p.pdf")
    doc.pdf_file.save("p.pdf", SimpleUploadedFile("p.pdf", b"%PDF"), save=True)
    resp = client.get(reverse("ui:upload_progress"), {"ids": str(doc.pk)})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["docs"][0]["id"] == str(doc.pk)
    assert payload["docs"][0]["done"] is False


@pytest.mark.django_db
def test_create_collection_is_root_specialty(client: Client) -> None:
    resp = client.post(reverse("ui:create_collection"), {"name": "Cardiología"})
    assert resp.status_code == 302
    c = Collection.objects.get(name="Cardiología")
    assert c.is_specialty_root is True
    assert c.parent is None
    assert f"collection={c.pk}" in resp["Location"]


@pytest.mark.django_db
def test_upload_into_collection(client: Client) -> None:
    col = Collection.objects.create(name="Oncología")
    f = SimpleUploadedFile("a.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = client.post(reverse("ui:upload"), {"pdfs": f, "collection": str(col.pk)})
    assert resp.status_code == 302
    doc = SourceDocument.objects.get()
    assert list(doc.collections.all()) == [col]


@pytest.mark.django_db
def test_library_filters_by_collection(client: Client) -> None:
    col = Collection.objects.create(name="Neuro")
    doc = SourceDocument(sha256="c" * 64)
    doc.pdf_file.save("x.pdf", SimpleUploadedFile("x.pdf", b"%PDF"), save=True)
    doc.collections.add(col)
    other = SourceDocument(sha256="d" * 64)
    other.pdf_file.save("y.pdf", SimpleUploadedFile("y.pdf", b"%PDF"), save=True)

    resp = client.get(reverse("ui:library"), {"collection": str(col.pk)})
    assert resp.status_code == 200
    assert str(doc.pk) in resp.content.decode()
    assert str(other.pk) not in resp.content.decode()


@pytest.mark.django_db
def test_una_especialidad_con_subcarpetas_muestra_sus_documentos_propios(client: Client) -> None:
    """Regresión: una especialidad con subespecialidades Y documentos propios (subida de PDFs
    sueltos a la colección activa) sigue mostrando esos documentos, no solo las tarjetas de
    subcarpeta. Antes el `{% if not children %}` de la plantilla los escondía por completo."""
    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    Collection.objects.create(name="Arritmias", parent=root)
    doc = SourceDocument(sha256="e" * 64)
    doc.pdf_file.save("x.pdf", SimpleUploadedFile("x.pdf", b"%PDF"), save=True)
    doc.collections.add(root)

    resp = client.get(reverse("ui:library"), {"collection": str(root.pk)})

    assert resp.status_code == 200
    assert str(doc.pk) in resp.content.decode()


@pytest.mark.django_db
def test_upload_with_subspecialty_creates_child(client: Client) -> None:
    parent = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    files = [
        SimpleUploadedFile("a.pdf", b"%PDF-1.4 a"),
        SimpleUploadedFile("b.pdf", b"%PDF-1.4 b"),
    ]
    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "subespecialidad",
            "parent_specialty": str(parent.pk),
            "subspecialty": "01_Arritmias-y-bloqueos",
            "pdfs": files,
        },
    )
    assert resp.status_code == 302
    sub = Collection.objects.get(parent=parent)
    assert sub.name == "Arritmias y bloqueos"
    assert f"collection={sub.pk}" in resp["Location"]
    assert SourceDocument.objects.filter(collections=sub).count() == 2


@pytest.mark.django_db
def test_upload_toda_la_especialidad_asocia_a_todas_las_subespecialidades(client: Client) -> None:
    esp = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    arritmias = Collection.objects.create(name="Arritmias", parent=esp)
    valvulopatias = Collection.objects.create(name="Valvulopatías", parent=esp)

    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "especialidad_todo",
            "collection": str(esp.pk),
            "pdfs": [SimpleUploadedFile("g.pdf", b"%PDF-1.4 g")],
        },
    )

    assert resp.status_code == 302
    assert f"collection={esp.pk}" in resp["Location"]
    doc = SourceDocument.objects.get()
    assert set(doc.collections.all()) == {arritmias, valvulopatias}


@pytest.mark.django_db
def test_upload_toda_la_especialidad_rechaza_un_id_que_no_es_raiz(client: Client) -> None:
    esp = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    sub = Collection.objects.create(name="Arritmias", parent=esp)

    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "especialidad_todo",
            "collection": str(sub.pk),
            "pdfs": [SimpleUploadedFile("g.pdf", b"%PDF-1.4 g")],
        },
    )

    assert resp.status_code == 302
    assert SourceDocument.objects.count() == 0


@pytest.mark.django_db
def test_sin_modo_los_ficheros_van_sueltos_a_la_coleccion_activa(client: Client) -> None:
    """El modo ya no se infiere de que venga `subspecialty`.

    Inferirlo es lo que hacía que una carpeta de especialidad entera acabase metida en una sola
    subespecialidad con el nombre de la carpeta raíz, que en el corpus es literalmente «PDF».
    """
    parent = Collection.objects.create(name="Cardiología", is_specialty_root=True)

    resp = client.post(
        reverse("ui:upload"),
        {
            "collection": str(parent.pk),
            "subspecialty": "Arritmias",
            "pdfs": [SimpleUploadedFile("a.pdf", b"%PDF-1.4 a")],
        },
    )

    assert resp.status_code == 302
    assert not Collection.objects.filter(name="Arritmias").exists()
    assert SourceDocument.objects.filter(collections=parent).count() == 1


@pytest.mark.django_db
def test_upload_without_subspecialty_goes_to_collection(client: Client) -> None:
    parent = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    resp = client.post(
        reverse("ui:upload"),
        {"collection": str(parent.pk), "pdfs": SimpleUploadedFile("a.pdf", b"%PDF-1.4 a")},
    )
    assert resp.status_code == 302
    assert Collection.objects.filter(parent=parent).count() == 0
    assert SourceDocument.objects.filter(collections=parent).count() == 1


@pytest.mark.django_db
def test_subspecialty_renders_prompt_and_norms(client: Client) -> None:
    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    sub = Collection.objects.create(
        name="Arritmias", parent=root, system_prompt="# Hola", norms=[{"id": "N-1"}]
    )
    resp = client.get(reverse("ui:library") + f"?collection={sub.pk}")
    content = resp.content.decode()
    assert "🧭 System prompt" in content
    assert "<h1>Hola</h1>" in content
    assert "📐 Normas" in content
    assert "N-1" in content


@pytest.mark.django_db
def test_subspecialty_without_prompt_shows_no_panels(client: Client) -> None:
    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    sub = Collection.objects.create(name="Arritmias", parent=root)
    resp = client.get(reverse("ui:library") + f"?collection={sub.pk}")
    assert 'class="sp-panel"' not in resp.content.decode()


@pytest.mark.django_db
def test_doc_type_counts_and_filter(client: Client) -> None:
    col = Collection.objects.create(name="Cardiología", is_specialty_root=True)

    def mk(sha: str, dt: str) -> None:
        d = SourceDocument(sha256=sha, doc_type=dt)
        d.pdf_file.save(f"{sha}.pdf", SimpleUploadedFile("x.pdf", b"%PDF"), save=True)
        d.collections.add(col)

    mk("a" * 64, "paper")
    mk("b" * 64, "paper")
    mk("c" * 64, "ficha_tecnica")

    resp = client.get(reverse("ui:library") + f"?collection={col.pk}")
    counts = {m["value"]: m["n"] for m in resp.context["type_metrics"]}
    assert counts["paper"] == 2
    assert counts["ficha_tecnica"] == 1
    assert counts["guia_clinica"] == 0
    assert "Ficha técnica" in resp.content.decode()

    resp = client.get(reverse("ui:library") + f"?collection={col.pk}&doc_type=paper")
    docs = list(resp.context["docs"])
    assert len(docs) == 2
    assert all(d.doc_type == "paper" for d in docs)


@pytest.mark.django_db
def test_home_shows_only_root_specialties(client: Client) -> None:
    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    Collection.objects.create(name="Arritmias", parent=root)
    resp = client.get(reverse("ui:library"))
    content = resp.content.decode()
    assert '<span class="nm">Cardiología</span>' in content
    assert '<span class="nm">Arritmias</span>' not in content


@pytest.mark.django_db
def test_detail_renders(client: Client) -> None:
    doc = SourceDocument(sha256="b" * 64)
    doc.pdf_file.save("x.pdf", SimpleUploadedFile("x.pdf", b"%PDF"), save=True)
    resp = client.get(reverse("ui:detail", args=[doc.pk]))
    assert resp.status_code == 200
    assert b"Ficha" in resp.content


@pytest.mark.django_db
def test_enrich_view_enqueues_task(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    from documents import tasks as doc_tasks

    doc = SourceDocument(sha256="9" * 64)
    doc.pdf_file.save("x.pdf", SimpleUploadedFile("x.pdf", b"%PDF"), save=True)

    enqueued: list[str] = []
    monkeypatch.setattr(
        doc_tasks.enrich_document_task, "delay", lambda doc_id: enqueued.append(doc_id)
    )
    resp = client.post(reverse("ui:enrich", args=[doc.pk]))
    assert resp.status_code == 302
    assert enqueued == [str(doc.pk)]


@pytest.mark.django_db
def test_upload_enqueues_enrichment(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    from documents import tasks as doc_tasks

    enqueued: list[str] = []
    monkeypatch.setattr(
        doc_tasks.enrich_document_task, "delay", lambda doc_id: enqueued.append(doc_id)
    )
    resp = client.post(
        reverse("ui:upload"),
        {"pdfs": SimpleUploadedFile("a.pdf", b"%PDF-1.4 a")},
    )
    assert resp.status_code == 302
    assert len(enqueued) == 1


def _espia_las_dos_colas(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    from documents import tasks as doc_tasks
    from pipeline import tasks as pipe_tasks

    colas: dict[str, list[str]] = {"enriquecer": [], "procesar": []}
    monkeypatch.setattr(
        doc_tasks.enrich_document_task, "delay", lambda i: colas["enriquecer"].append(i)
    )
    monkeypatch.setattr(
        pipe_tasks.process_document_task, "delay", lambda i: colas["procesar"].append(i)
    )
    return colas


@pytest.mark.django_db
def test_subir_sin_marcar_la_casilla_no_procesa(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desmarcada por defecto: una carpeta son cientos de PDFs, y cada uno gasta LLM y una JVM."""
    colas = _espia_las_dos_colas(monkeypatch)

    client.post(reverse("ui:upload"), {"pdfs": SimpleUploadedFile("a.pdf", b"%PDF-1.4 a")})

    assert len(colas["enriquecer"]) == 1
    assert colas["procesar"] == []


@pytest.mark.django_db
def test_subir_con_la_casilla_marcada_encola_el_pipeline(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    colas = _espia_las_dos_colas(monkeypatch)

    client.post(
        reverse("ui:upload"),
        {"pdfs": SimpleUploadedFile("a.pdf", b"%PDF-1.4 a"), "process_now": "1"},
    )

    assert len(colas["enriquecer"]) == 1
    assert colas["procesar"] == colas["enriquecer"]


@pytest.mark.django_db
def test_con_la_casilla_marcada_se_encolan_todos_los_del_lote(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un `delay` fuera del bucle procesaría solo el último del lote."""
    colas = _espia_las_dos_colas(monkeypatch)

    client.post(
        reverse("ui:upload"),
        {
            "pdfs": [
                SimpleUploadedFile("a.pdf", b"%PDF-1.4 a"),
                SimpleUploadedFile("b.pdf", b"%PDF-1.4 b"),
                SimpleUploadedFile("c.pdf", b"%PDF-1.4 c"),
            ],
            "process_now": "1",
        },
    )

    assert len(colas["procesar"]) == 3


@pytest.mark.django_db
def test_la_casilla_aparece_desmarcada_en_la_biblioteca(client: Client) -> None:
    """El formulario de subida solo se renderiza con una colección activa."""
    col = Collection.objects.create(name="Cardiología", is_specialty_root=True)

    html = client.get(reverse("ui:library"), {"collection": str(col.pk)}).content.decode()

    assert 'name="process_now"' in html
    assert "checked" not in html.split('name="process_now"')[1].split(">")[0]


@pytest.mark.django_db
def test_index_view_enqueues_task(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import index as index_mod

    doc = SourceDocument(sha256="f" * 64)
    doc.pdf_file.save("x.pdf", SimpleUploadedFile("x.pdf", b"%PDF"), save=True)

    enqueued: list[str] = []
    monkeypatch.setattr(
        index_mod.index_document_task, "delay", lambda doc_id: enqueued.append(doc_id)
    )
    resp = client.post(reverse("ui:index", args=[doc.pk]))
    assert resp.status_code == 302
    assert enqueued == [str(doc.pk)]


@pytest.mark.django_db
def test_index_view_rejects_partial_extraction(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El control que impide meter en el ChromaDB compartido lo que `tasks.py` rechazó.

    La plantilla ya no ofrece el botón, pero un POST directo llega igual a la vista, y la guarda
    de `index_document` corre en el worker: sin esta, el rechazo dependía de que nadie mandara el
    POST a mano.
    """
    from pipeline import index as index_mod

    doc = _doc("e" * 64, status=Status.FAILED, json=True)
    doc.error_stage = Stage.JSON
    doc.save(update_fields=["error_stage"])

    enqueued: list[str] = []
    monkeypatch.setattr(
        index_mod.index_document_task, "delay", lambda doc_id: enqueued.append(doc_id)
    )
    resp = client.post(reverse("ui:index", args=[doc.pk]))

    assert resp.status_code == 302
    assert enqueued == []


@pytest.mark.django_db
def test_detail_hides_index_button_for_partial_extraction(client: Client) -> None:
    """El contrapunto en la plantilla: un fallo de indexado sí conserva el botón."""
    parcial = _doc("d" * 64, status=Status.FAILED, json=True)
    parcial.error_stage = Stage.JSON
    parcial.save(update_fields=["error_stage"])

    fallo_indexado = _doc("c" * 64, status=Status.FAILED, json=True)
    fallo_indexado.error_stage = Stage.INDEX
    fallo_indexado.save(update_fields=["error_stage"])

    html_parcial = client.get(reverse("ui:detail", args=[parcial.pk])).content.decode()
    html_indexado = client.get(reverse("ui:detail", args=[fallo_indexado.pk])).content.decode()

    assert reverse("ui:index", args=[parcial.pk]) not in html_parcial
    assert reverse("ui:index", args=[fallo_indexado.pk]) in html_indexado


@pytest.mark.django_db
def test_serve_pdf_returns_pdf(client: Client) -> None:
    doc = _doc("a" * 64)
    resp = client.get(reverse("ui:pdf", args=[doc.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf"


@pytest.mark.django_db
def test_add_and_remove_tag(client: Client) -> None:
    doc = _doc("b" * 64)
    resp = client.post(reverse("ui:add_tag", args=[doc.pk]), {"name": "revisar"})
    assert resp.status_code == 302
    tag = Tag.objects.get(name="revisar")
    assert tag in doc.tags.all()

    resp = client.post(reverse("ui:remove_tag", args=[doc.pk, tag.pk]))
    assert resp.status_code == 302
    assert tag not in doc.tags.all()
    assert Tag.objects.filter(pk=tag.pk).exists()


@pytest.mark.django_db
def test_library_filters_by_tag(client: Client) -> None:
    col = Collection.objects.create(name="Neuro")
    tag = Tag.objects.create(name="urgente")
    d1 = _doc("c" * 64)
    d1.collections.add(col)
    d1.tags.add(tag)
    d2 = _doc("d" * 64)
    d2.collections.add(col)

    resp = client.get(reverse("ui:library"), {"collection": str(col.pk), "tag": str(tag.pk)})
    content = resp.content.decode()
    assert str(d1.pk) in content
    assert str(d2.pk) not in content


@pytest.mark.django_db
def test_dashboard_renders_with_counts(client: Client) -> None:
    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    d = _doc("e" * 64, status=Status.INDEXED, json=True)
    d.collections.add(root)
    resp = client.get(reverse("ui:dashboard"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Reconciliación" in content
    assert "Cardiología" in content


@pytest.mark.django_db
def test_retry_failed_reenqueues_by_stage(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import index as index_mod
    from pipeline import tasks as pipe_tasks

    idx: list[str] = []
    proc: list[str] = []
    monkeypatch.setattr(index_mod.index_document_task, "delay", lambda i: idx.append(i))
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: proc.append(i))

    d_index = _doc("1" * 64, status=Status.FAILED)
    d_index.error_stage = Stage.INDEX
    d_index.save()
    d_parse = _doc("2" * 64, status=Status.FAILED)
    d_parse.error_stage = Stage.PARSE
    d_parse.save()

    resp = client.post(reverse("ui:retry_failed"))
    assert resp.status_code == 302
    assert idx == [str(d_index.pk)]
    assert proc == [str(d_parse.pk)]


@pytest.mark.django_db
def test_retry_failed_scoped_to_specialty(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import tasks as pipe_tasks

    proc: list[str] = []
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: proc.append(i))

    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    in_spec = _doc("3" * 64, status=Status.FAILED)
    in_spec.collections.add(root)
    _doc("4" * 64, status=Status.FAILED)

    resp = client.post(reverse("ui:retry_failed"), {"specialty": "cardiología"})
    assert resp.status_code == 302
    assert proc == [str(in_spec.pk)]


@pytest.mark.django_db
def test_create_snapshot_and_timeline(client: Client) -> None:
    _doc("a" * 64, status=Status.INDEXED)
    resp = client.post(reverse("ui:create_snapshot"), {"label": "v1"})
    assert resp.status_code == 302
    assert CorpusSnapshot.objects.filter(label="v1").exists()

    resp = client.get(reverse("ui:timeline"))
    assert resp.status_code == 200
    assert b"v1" in resp.content


@pytest.mark.django_db
def test_snapshot_detail_renders(client: Client) -> None:
    from pipeline.snapshots import create_snapshot

    _doc("b" * 64, status=Status.INDEXED)
    snap = create_snapshot(label="v1")
    resp = client.get(reverse("ui:snapshot_detail", args=[snap.pk]))
    assert resp.status_code == 200
    assert b"Estado del corpus" in resp.content


@pytest.mark.django_db
def test_snapshot_compare_renders(client: Client) -> None:
    from pipeline.snapshots import create_snapshot

    _doc("1" * 64)
    older = create_snapshot(label="v1")
    alta = _doc("2" * 64)
    newer = create_snapshot(label="v2")

    resp = client.get(reverse("ui:snapshot_compare"), {"a": str(older.pk), "b": str(newer.pk)})
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Altas" in content
    assert alta.sha256[:12] in content


@pytest.mark.django_db
def test_la_comparacion_distingue_dos_fallos_de_etapa_distinta(client: Client) -> None:
    """Sin la etapa en la fila, el diff reportaba un cambio y la tabla pintaba dos valores iguales.

    `FAILED · 0 chunks → FAILED · 0 chunks`: una fila que afirma que algo cambió y no dice qué. Y
    era justo la distinción que motivó llevar la etapa al manifiesto — un JSON bueno que no llegó
    a Chroma frente a uno incompleto que no debe indexarse.
    """
    from pipeline.snapshots import create_snapshot

    doc = _doc("7" * 64, status=Status.FAILED)
    doc.error_stage = Stage.INDEX
    doc.save(update_fields=["error_stage"])
    older = create_snapshot(label="v1")

    doc.error_stage = Stage.JSON
    doc.save(update_fields=["error_stage"])
    newer = create_snapshot(label="v2")

    resp = client.get(reverse("ui:snapshot_compare"), {"a": str(older.pk), "b": str(newer.pk)})

    content = resp.content.decode()
    assert f"en {Stage.INDEX}" in content
    assert f"en {Stage.JSON}" in content


@pytest.mark.django_db
def test_el_detalle_del_snapshot_muestra_la_etapa_del_fallo(client: Client) -> None:
    """El dato existe para ser visto: capturarlo y no pintarlo deja el hallazgo sin cerrar."""
    from pipeline.snapshots import create_snapshot

    doc = _doc("8" * 64, status=Status.FAILED)
    doc.error_stage = Stage.JSON
    doc.save(update_fields=["error_stage"])
    snap = create_snapshot(label="v1")

    resp = client.get(reverse("ui:snapshot_detail", args=[snap.pk]))

    assert f"en {Stage.JSON}" in resp.content.decode()


@pytest.mark.django_db
def test_un_documento_sin_fallo_no_pinta_etapa(client: Client) -> None:
    """El contrapunto: un `INDEXED` no lleva etapa, y la fila no puede decir «en »."""
    from pipeline.snapshots import create_snapshot

    _doc("9" * 64, status=Status.INDEXED)
    snap = create_snapshot(label="v1")

    resp = client.get(reverse("ui:snapshot_detail", args=[snap.pk]))

    assert "en </span>" not in resp.content.decode()
    assert ">en " not in resp.content.decode()


@pytest.mark.django_db
def test_la_biblioteca_filtra_por_estado_dentro_de_una_coleccion(client: Client) -> None:
    """El filtro de estado y el de colección se combinan; antes solo se probaba cada uno suelto."""
    root = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    indexado = _doc("a" * 64, status=Status.INDEXED)
    fallido = _doc("b" * 64, status=Status.FAILED)
    for doc in (indexado, fallido):
        doc.collections.add(root)

    resp = client.get(f"{reverse('ui:library')}?collection={root.pk}&status={Status.FAILED}")

    contenido = resp.content.decode()
    assert str(fallido.pk) in contenido
    assert str(indexado.pk) not in contenido


@pytest.mark.django_db
def test_comparar_snapshots_ordena_por_fecha_aunque_se_pasen_al_reves(client: Client) -> None:
    """La UI enlaza `a` y `b` desde una tabla que el usuario puede ordenar como quiera.

    Sin el intercambio, comparar el nuevo contra el viejo invierte altas y bajas: los documentos
    añadidos se reportarían como borrados del corpus.
    """
    from pipeline.snapshots import create_snapshot

    viejo = create_snapshot(label="v1")
    _doc("c" * 64, status=Status.INDEXED)
    nuevo = create_snapshot(label="v2")

    resp = client.get(f"{reverse('ui:snapshot_compare')}?a={nuevo.pk}&b={viejo.pk}")

    assert resp.status_code == 200
    assert resp.context["older"].pk == viejo.pk
    assert resp.context["newer"].pk == nuevo.pk


@pytest.mark.django_db
def test_pedir_el_pdf_de_un_documento_sin_fichero_da_404(client: Client) -> None:
    """`SourceDocument` puede existir sin PDF: el 404 evita un 500 en la previsualización."""
    doc = SourceDocument.objects.create(sha256="d" * 64)

    assert client.get(reverse("ui:pdf", args=[doc.pk])).status_code == 404


@pytest.mark.django_db
def test_crear_una_coleccion_sin_nombre_vuelve_a_la_biblioteca_sin_crear_nada(
    client: Client,
) -> None:
    """Un nombre en blanco crearía una especialidad anónima imposible de distinguir."""
    resp = client.post(reverse("ui:create_collection"), {"name": "   "})

    assert resp.status_code == 302
    assert resp["Location"] == reverse("ui:library")
    assert not Collection.objects.exists()


@pytest.mark.django_db
def test_un_fichero_rechazado_no_aborta_el_resto_de_la_subida(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una carpeta son cientos de PDFs: que uno no sea válido no puede tirar el lote entero.

    Es el `continue` del `except PdfRejected`, y sin él subir una carpeta con un `.DS_Store`
    dentro perdía todos los documentos posteriores al rechazado.
    """
    resp = client.post(
        reverse("ui:upload"),
        {
            "pdfs": [
                SimpleUploadedFile("malo.txt", b"esto no es un PDF"),
                SimpleUploadedFile("bueno.pdf", b"%PDF-1.4 contenido"),
            ]
        },
    )

    assert resp.status_code == 302
    superviviente = SourceDocument.objects.get()
    assert "malo" not in superviviente.original_filename.lower()


@pytest.mark.django_db
def test_procesar_desde_el_detalle_encola_la_etapa_pdf_a_json(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El botón «Procesar» del detalle: es la vía manual cuando la subida no marcó la casilla."""
    from pipeline import tasks as pipe_tasks

    doc = _doc("e" * 64)
    encolados: list[str] = []
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: encolados.append(i))

    resp = client.post(reverse("ui:process", args=[doc.pk]))

    assert resp.status_code == 302
    assert encolados == [str(doc.pk)]


@pytest.mark.django_db
def test_un_get_al_endpoint_de_proceso_no_encola_nada(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Procesar gasta JVM y LLM: un GET —un prefetch del navegador— no puede dispararlo."""
    from pipeline import tasks as pipe_tasks

    doc = _doc("f" * 64)
    encolados: list[str] = []
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: encolados.append(i))

    resp = client.get(reverse("ui:process", args=[doc.pk]))

    assert resp.status_code == 302
    assert encolados == []


@pytest.mark.django_db
def test_procesar_un_documento_ya_procesado_lo_regenera(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El botón «Regenerar» sobre un documento ya convertido lo reinicia a UPLOADED y lo reencola:
    así el reproceso pedido a mano sí corre, mientras la reentrega de la cola —que no reinicia— se
    sigue omitiendo."""
    from pipeline import tasks as pipe_tasks

    doc = _doc("d" * 64, status=Status.JSON_READY, json=True)
    encolados: list[str] = []
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: encolados.append(i))

    resp = client.post(reverse("ui:process", args=[doc.pk]))

    assert resp.status_code == 302
    assert encolados == [str(doc.pk)]
    doc.refresh_from_db()
    assert doc.status == Status.UPLOADED


def _ficha_de_cima(pdf_url: object) -> SourceDocument:
    import json

    doc = SourceDocument.objects.create(
        sha256="c" * 64, status=Status.JSON_READY, doc_type=DocType.FICHA_TECNICA
    )
    cuerpo = json.dumps({"posology": "10 mg", "_source": {"pdf_url": pdf_url}}).encode()
    doc.json_file.save("ficha.json", SimpleUploadedFile("ficha.json", cuerpo), save=True)
    CimaRegistration.objects.create(
        nregistro="75831", document=doc, content_hash="c" * 64, es_canonico=True
    )
    return doc


@pytest.mark.django_db
def test_una_ficha_de_cima_no_se_regenera_desde_un_pdf(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import tasks as pipe_tasks

    doc = _ficha_de_cima("https://cima.aemps.es/cima/pdfs/ft/75831/FT_75831.pdf")
    encolados: list[str] = []
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: encolados.append(i))

    from django.contrib.messages import get_messages

    resp = client.post(reverse("ui:process", args=[doc.pk]))

    assert resp.status_code == 302
    assert encolados == []
    assert ["se actualiza desde CIMA" in str(m) for m in get_messages(resp.wsgi_request)] == [True]
    doc.refresh_from_db()
    assert doc.status == Status.JSON_READY


@pytest.mark.django_db
def test_el_detalle_de_una_ficha_de_cima_enlaza_su_pdf_oficial_y_no_ofrece_procesar(
    client: Client,
) -> None:
    url = "https://cima.aemps.es/cima/pdfs/ft/75831/FT_75831.pdf"
    doc = _ficha_de_cima(url)

    html = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert f'href="{url}"' in html
    assert "(PDF → JSON)" not in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    "pdf_url",
    [
        "http://cima.aemps.es/cima/pdfs/ft/75831/FT_75831.pdf",
        "https://cima.aemps.es.de-un-tercero.example/FT.pdf",
        "https://otro.example/FT.pdf",
        None,
        42,
        "https://[cima.aemps.es/FT.pdf",
    ],
)
def test_un_enlace_al_pdf_que_no_es_de_cima_no_se_pinta(client: Client, pdf_url: object) -> None:
    doc = _ficha_de_cima(pdf_url)

    html = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "Ficha técnica oficial" not in html
    assert "(PDF → JSON)" not in html


def test_el_enlace_al_pdf_solo_sale_de_un_json_con_bloque_source() -> None:
    from ui.views import _pdf_oficial

    assert _pdf_oficial(None) is None
    assert _pdf_oficial({"_source": "no es un objeto"}) is None


def _upload_pdf(nombre: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(nombre, b"%PDF-1.4 " + nombre.encode())


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["ui:library", "ui:dashboard", "ui:timeline"])
def test_ninguna_pagina_escupe_sintaxis_de_plantilla(client: Client, url_name: str) -> None:
    """`{# #}` es de **una sola línea**: uno de varias se imprime tal cual en la página.

    Pasó de verdad con los comentarios del widget de subida, y no lo veía nadie: la página sigue
    dando 200 y el texto se cuela entre el contenido. Ningún test de los que había miraba el HTML
    en busca de sintaxis sin resolver.
    """
    contenido = client.get(reverse(url_name)).content.decode()

    assert "{#" not in contenido
    assert "{%" not in contenido
    assert "{{" not in contenido


@pytest.mark.django_db
def test_el_widget_de_subida_esta_tambien_en_la_portada(client: Client) -> None:
    """El modo «carpeta de especialidad» **crea** la especialidad.

    Si el widget solo existiera dentro de una, para subirla habría que haberla creado antes: el
    flujo sería circular y la subida por carpeta no serviría para arrancar un corpus.
    """
    contenido = client.get(reverse("ui:library")).content.decode()

    assert 'id="uploadform"' in contenido
    assert 'value="especialidad"' in contenido


@pytest.mark.django_db
def test_subir_una_carpeta_de_especialidad_crea_sus_subespecialidades(client: Client) -> None:
    """El modo lo declara quien sube; antes se infería de si venía `subspecialty`."""
    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "especialidad",
            "specialty_name": "Alergología",
            "pdfs": [_upload_pdf("a.pdf"), _upload_pdf("b.pdf"), _upload_pdf("c.pdf")],
            "rel_paths": ["Anafilaxia/a.pdf", "Conjuntivitis/b.pdf", "c.pdf"],
        },
    )

    assert resp.status_code == 302
    esp = Collection.objects.get(name="Alergología", parent__isnull=True)
    assert sorted(c.name for c in esp.children.all()) == ["Anafilaxia", "Conjuntivitis"]
    assert esp.documents.count() == 1
    assert f"collection={esp.pk}" in resp["Location"]


@pytest.mark.django_db
def test_una_carpeta_de_especialidad_sin_nombre_no_sube_nada(client: Client) -> None:
    """El nombre se teclea y es obligatorio: la carpeta del corpus se llama literalmente «PDF»."""
    resp = client.post(
        reverse("ui:upload"),
        {"modo": "especialidad", "specialty_name": "  ", "pdfs": [_upload_pdf("a.pdf")]},
    )

    assert resp.status_code == 302
    assert Collection.objects.count() == 0
    assert SourceDocument.objects.count() == 0


@pytest.mark.django_db
def test_subir_una_subespecialidad_a_una_especialidad_existente(client: Client) -> None:
    esp = Collection.objects.create(name="Alergología", is_specialty_root=True)

    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "subespecialidad",
            "parent_specialty": str(esp.pk),
            "subspecialty": "Anafilaxia",
            "pdfs": [_upload_pdf("a.pdf")],
        },
    )

    assert resp.status_code == 302
    sub = Collection.objects.get(name="Anafilaxia")
    assert sub.parent == esp
    assert sub.is_specialty_root is False


@pytest.mark.django_db
def test_subir_una_subespecialidad_creando_la_especialidad_al_vuelo(client: Client) -> None:
    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "subespecialidad",
            "parent_specialty": "",
            "new_specialty_name": "Alergología",
            "subspecialty": "Anafilaxia",
            "pdfs": [_upload_pdf("a.pdf")],
        },
    )

    assert resp.status_code == 302
    esp = Collection.objects.get(name="Alergología", parent__isnull=True)
    assert esp.is_specialty_root is True
    assert [c.name for c in esp.children.all()] == ["Anafilaxia"]


@pytest.mark.django_db
def test_una_subespecialidad_sin_especialidad_no_se_cuelga_de_la_raiz(client: Client) -> None:
    """Sin especialidad no hay sitio: colgarla de la raíz la convertiría en una especialidad."""
    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "subespecialidad",
            "parent_specialty": "",
            "new_specialty_name": "",
            "subspecialty": "Anafilaxia",
            "pdfs": [_upload_pdf("a.pdf")],
        },
    )

    assert resp.status_code == 302
    assert Collection.objects.count() == 0
    assert SourceDocument.objects.count() == 0


@pytest.mark.django_db
def test_una_especialidad_padre_inexistente_no_sube_nada(client: Client) -> None:
    """El id viaja en el formulario: uno que no exista, o que sea una subespecialidad, no vale."""
    esp = Collection.objects.create(name="Alergología", is_specialty_root=True)
    sub = Collection.objects.create(name="Anafilaxia", parent=esp)

    resp = client.post(
        reverse("ui:upload"),
        {
            "modo": "subespecialidad",
            "parent_specialty": str(sub.pk),
            "subspecialty": "Urticaria",
            "pdfs": [_upload_pdf("a.pdf")],
        },
    )

    assert resp.status_code == 302
    assert not Collection.objects.filter(name="Urticaria").exists()
    assert SourceDocument.objects.count() == 0


@pytest.mark.django_db
def test_los_ficheros_descartados_se_le_dicen_a_quien_sube(client: Client, settings: Any) -> None:
    """En una carpeta de cientos, el que excede el tope desaparecía sin dejar rastro."""
    settings.MAX_PDF_UPLOAD_BYTES = 32

    client.post(
        reverse("ui:upload"),
        {
            "modo": "especialidad",
            "specialty_name": "Alergología",
            "pdfs": [
                _upload_pdf("cabe.pdf"),
                SimpleUploadedFile("enorme.pdf", b"%PDF-1.4 " + b"x" * 200),
            ],
            "rel_paths": ["Anafilaxia/cabe.pdf", "Anafilaxia/enorme.pdf"],
        },
    )
    pagina = client.get(reverse("ui:library")).content.decode()

    assert "enorme.pdf" in pagina
    assert "no se subió" in pagina


@pytest.mark.django_db
def test_el_aviso_de_descartados_no_se_repite_al_recargar(client: Client, settings: Any) -> None:
    """Va en la sesión y se consume al leerlo: si no, acompaña al usuario indefinidamente."""
    settings.MAX_PDF_UPLOAD_BYTES = 32
    client.post(
        reverse("ui:upload"),
        {
            "modo": "especialidad",
            "specialty_name": "Alergología",
            "pdfs": [SimpleUploadedFile("enorme.pdf", b"%PDF-1.4 " + b"x" * 200)],
            "rel_paths": ["Anafilaxia/enorme.pdf"],
        },
    )

    assert "enorme.pdf" in client.get(reverse("ui:library")).content.decode()
    assert "enorme.pdf" not in client.get(reverse("ui:library")).content.decode()


def _informe(doc: SourceDocument, *, peor: float, global_: float = 1.0) -> FidelityReport:
    return FidelityReport.from_engine(
        doc,
        {
            "report_version": 1,
            "variant": "guia_clinica",
            "doc_chars": 1000,
            "conversion": {"short_headings": 3, "headings_total": 14, "other_clinical_ratio": 0.2},
            "coverage": {
                "global_coverage": global_,
                "uncovered_chars": 0,
                "sections": {
                    "recommendations": {
                        "coverage": peor,
                        "covered_doc_chars": int(peor * 1000),
                        "truncated_chars": 0,
                    }
                },
            },
        },
    )


@pytest.mark.django_db
def test_la_pagina_de_fidelidad_ordena_por_la_peor_seccion(client: Client) -> None:
    """Ordenar por la cobertura global habría dejado el documento dañado al final de la lista.

    Los dos tienen unión global del 100% —es lo que pasaba en la pérdida real, porque otras
    secciones reciben el documento entero—, así que solo la peor sección los distingue.
    """
    sano = _doc("1" * 64)
    sano.title = "Guía sana"
    sano.save(update_fields=["title"])
    danado = _doc("2" * 64)
    danado.title = "Guía con pérdida"
    danado.save(update_fields=["title"])
    _informe(sano, peor=0.81)
    _informe(danado, peor=0.14)

    pagina = client.get(reverse("ui:fidelity")).content.decode()

    assert pagina.index("Guía con pérdida") < pagina.index("Guía sana")


@pytest.mark.django_db
def test_la_pagina_de_fidelidad_solo_muestra_el_ultimo_informe_de_cada_documento(
    client: Client,
) -> None:
    """Un documento reprocesado enterraría al resto bajo su propio historial."""
    doc = _doc("3" * 64)
    _informe(doc, peor=0.14)
    _informe(doc, peor=0.92)

    pagina = client.get(reverse("ui:fidelity")).content.decode()

    assert "0,92" in pagina
    assert "0,14" not in pagina


@pytest.mark.django_db
def test_el_listado_y_el_detalle_eligen_el_mismo_informe_ante_un_empate(client: Client) -> None:
    """Sin desempate, cada vista podía enseñar un informe distinto del mismo documento.

    Los dos ordenan por fecha; con dos informes en el mismo instante el motor devolvía
    cualquiera de ellos, y el listado decía una cobertura y el detalle otra. El desempate por
    `pk` es arbitrario a propósito: lo que importa es que las dos vistas coincidan.
    """
    doc = _doc("7" * 64)
    primero = _informe(doc, peor=0.14)
    segundo = _informe(doc, peor=0.92)
    FidelityReport.objects.filter(pk__in=[primero.pk, segundo.pk]).update(
        created_at=primero.created_at
    )

    listado = client.get(reverse("ui:fidelity")).content.decode()
    detalle = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    elegido = "0,92" if "0,92" in listado else "0,14"
    assert elegido in detalle


@pytest.mark.django_db
def test_el_texto_duplicado_se_ve_en_el_listado_y_en_el_detalle(client: Client) -> None:
    """De nada sirve detectarlo si no se ve: es el fallo que nadie vio en el corpus congelado."""
    doc = _doc("d" * 64)
    FidelityReport.from_engine(
        doc,
        {
            "report_version": 2,
            "variant": "ficha_tecnica",
            "path": "cima",
            "registration_number": "61905",
            "doc_chars": 1000,
            "conversion": None,
            "coverage": {
                "global_coverage": 0.9,
                "uncovered_chars": 0,
                "sections": {"posology": {"coverage": 1.0, "chunk_chars": 0}},
            },
            "missing_fields": [],
            "duplicated_fields": {"pregnancy_and_lactation": 1103},
        },
    )

    listado = client.get(reverse("ui:fidelity")).content.decode()
    detalle = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "texto duplicado" in listado
    assert "pregnancy_and_lactation (1103 car.)" in listado
    assert "archivado dos veces" in detalle
    assert "pregnancy_and_lactation (1103 car.)" in detalle


@pytest.mark.django_db
def test_la_entrada_sin_cubrir_se_marca_en_el_listado_y_en_el_detalle(client: Client) -> None:
    """Antes la pérdida de entrada vivía en un pie de página del detalle: una guía que no cabe en
    la ventana del modelo se indexaba incompleta sin que nada lo dijera donde se revisa."""
    doc = _doc("e" * 64)
    doc.title = "Guía enorme"
    doc.save(update_fields=["title"])
    FidelityReport.from_engine(
        doc,
        {
            "report_version": 1,
            "variant": "guia_clinica",
            "doc_chars": 500_000,
            "conversion": {"short_headings": 0, "headings_total": 99, "other_clinical_ratio": 0.3},
            "coverage": {
                "global_coverage": 0.32,
                "uncovered_chars": 340_000,
                "sections": {
                    "safety": {
                        "coverage": 0.12,
                        "covered_doc_chars": 60_000,
                        "truncated_chars": 235_000,
                    }
                },
            },
        },
    )

    listado = client.get(reverse("ui:fidelity")).content.decode()
    detalle = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "Entrada sin cubrir" in listado
    assert "0,68" in listado
    assert "no llegaron a ninguna sección" in detalle
    assert "0,68" in detalle


@pytest.mark.django_db
def test_la_pagina_de_fidelidad_lo_dice_cuando_no_hay_informes(client: Client) -> None:
    pagina = client.get(reverse("ui:fidelity")).content.decode()

    assert "Aún no hay informes" in pagina


@pytest.mark.django_db
def test_el_detalle_muestra_la_cobertura_por_seccion(client: Client) -> None:
    doc = _doc("4" * 64)
    _informe(doc, peor=0.14)

    pagina = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "Fidelidad de la extracción" in pagina
    assert "recommendations" in pagina
    assert "0,14" in pagina, "la UI va en español: Django localiza el separador decimal"


@pytest.mark.django_db
def test_las_secciones_omitidas_de_la_capa_3_se_ven_en_el_listado_y_en_el_detalle(
    client: Client,
) -> None:
    doc = _doc("c" * 64)
    doc.title = "Guía con omisión"
    doc.save(update_fields=["title"])
    FidelityReport.from_engine(
        doc,
        {
            "report_version": 4,
            "variant": "guia_clinica",
            "doc_chars": 1000,
            "conversion": {"short_headings": 0, "headings_total": 5, "other_clinical_ratio": 0.2},
            "coverage": {
                "global_coverage": 1.0,
                "uncovered_chars": 0,
                "sections": {
                    "recommendations": {
                        "coverage": 1.0,
                        "covered_doc_chars": 1000,
                        "truncated_chars": 0,
                    }
                },
            },
            "salient": {
                "salient_terms_per_section": 12,
                "sections": {
                    "2:Therapies": {
                        "category": "other_clinical",
                        "salient_terms": 12,
                        "present": 0,
                        "recall": 0.0,
                    }
                },
                "omitted_sections": ["2:Therapies"],
            },
        },
    )

    listado = client.get(reverse("ui:fidelity")).content.decode()
    detalle = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "1 de 1" in listado
    assert "Recuperación de lo saliente" in detalle
    assert "2:Therapies" in detalle
    assert "no dejaron ningún término" in detalle


@pytest.mark.django_db
def test_el_detalle_de_un_documento_sin_informe_no_pinta_el_bloque(client: Client) -> None:
    doc = _doc("5" * 64)

    pagina = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "Fidelidad de la extracción" not in pagina


@pytest.mark.django_db
def test_el_detalle_muestra_el_ultimo_informe_y_no_el_primero(client: Client) -> None:
    doc = _doc("6" * 64)
    _informe(doc, peor=0.14)
    _informe(doc, peor=0.92)

    pagina = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert "0,92" in pagina
    assert "0,14" not in pagina


@pytest.mark.django_db
def test_serve_md_devuelve_el_markdown_intermedio(client: Client) -> None:
    doc = _doc("a1" + "0" * 62)
    doc.md_file.save("x.md", SimpleUploadedFile("x.md", b"# Titulo\n\ntexto"), save=True)

    resp = client.get(reverse("ui:md", args=[doc.pk]))

    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/markdown; charset=utf-8"
    assert resp["Cache-Control"] == "private, no-store"
    assert "attachment" in resp["Content-Disposition"]
    assert b"".join(resp.streaming_content) == b"# Titulo\n\ntexto"  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_serve_md_404_sin_markdown(client: Client) -> None:
    doc = _doc("b2" + "0" * 62)

    assert client.get(reverse("ui:md", args=[doc.pk])).status_code == 404


@pytest.mark.django_db
def test_dashboard_data_devuelve_el_estado_en_json(client: Client) -> None:
    card = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    _doc("c3" + "0" * 62, status=Status.JSON_READY, json=True).collections.add(card)

    resp = client.get(reverse("ui:dashboard_data"))

    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["total"] == 1
    assert data["totals"]["done"] == 1
    assert data["totals"]["pct"] == 100


@pytest.mark.django_db
def test_el_eta_no_cuenta_los_documentos_fallidos_como_pendientes(client: Client) -> None:
    """Un FAILED es terminal, no trabajo pendiente. Con lo no hecho todo en FAILED y conversiones
    recientes (per_hour > 0), el ETA es None en vez de marcar «faltan N h» para siempre."""
    card = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    _doc("f5" + "0" * 62, status=Status.JSON_READY, json=True).collections.add(card)
    _doc("f6" + "0" * 62, status=Status.FAILED).collections.add(card)

    data = client.get(reverse("ui:dashboard_data")).json()

    assert data["totals"]["failed"] == 1
    assert data["eta_hours"] is None


@pytest.mark.django_db
def test_dashboard_data_memoiza_la_reconciliacion_entre_polls(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El polling recalculaba la reconciliación (barrido de todos los documentos) en cada
    petición. Ahora se memoiza: dos polls seguidos la calculan una sola vez."""
    card = Collection.objects.create(name="Cardiología", is_specialty_root=True)
    _doc("g7" + "0" * 62, status=Status.JSON_READY, json=True).collections.add(card)
    from documents.reconciliation import reconciliation_by_specialty as real

    llamadas = {"n": 0}

    def _contando() -> Any:
        llamadas["n"] += 1
        return real()

    monkeypatch.setattr("ui.views.reconciliation_by_specialty", _contando)

    client.get(reverse("ui:dashboard_data"))
    client.get(reverse("ui:dashboard_data"))

    assert llamadas["n"] == 1


@pytest.mark.django_db
def test_doc_progress_devuelve_estado_y_avance(client: Client) -> None:
    doc = _doc("d4" + "0" * 62, status=Status.PARSING)
    doc.progress_done, doc.progress_total, doc.progress_label = 2, 5, "clinical"
    doc.save()

    resp = client.get(reverse("ui:doc_progress", args=[doc.pk]))

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == Status.PARSING
    assert (data["done"], data["total"], data["label"], data["pct"]) == (2, 5, "clinical", 40)


@pytest.mark.django_db
def test_el_detalle_renderiza_el_markdown_intermedio(client: Client) -> None:
    doc = _doc("e5" + "0" * 62)
    doc.md_file.save("x.md", SimpleUploadedFile("x.md", b"# Encabezado\n\nparrafo"), save=True)

    html = client.get(reverse("ui:detail", args=[doc.pk])).content.decode()

    assert 'data-tab="md"' in html
    assert "<h1>Encabezado</h1>" in html


@pytest.mark.django_db
def test_el_detalle_tolera_un_json_ilegible(client: Client) -> None:
    """El visor es best-effort: un JSON corrupto deja la pestaña vacía, no tumba el detalle."""
    doc = _doc("f6" + "0" * 62)
    doc.json_file.save("x.json", SimpleUploadedFile("x.json", b"{no es json"), save=True)

    resp = client.get(reverse("ui:detail", args=[doc.pk]))

    assert resp.status_code == 200
    assert 'data-tab="json"' not in resp.content.decode()


@pytest.mark.django_db
def test_el_detalle_tolera_un_markdown_ilegible(client: Client) -> None:
    """Igual con el Markdown: bytes que no decodifican en UTF-8 dejan la pestaña MD vacía."""
    doc = _doc("a7" + "0" * 62)
    doc.md_file.save("x.md", SimpleUploadedFile("x.md", b"\xff\xfe not utf-8"), save=True)

    resp = client.get(reverse("ui:detail", args=[doc.pk]))

    assert resp.status_code == 200
    assert 'data-tab="md"' not in resp.content.decode()


@pytest.mark.django_db
def test_la_biblioteca_no_lista_el_bucket_de_fichas_como_especialidad(client: Client) -> None:
    """El bucket de fichas es una raíz, pero no una especialidad, así que no sale en el lateral.

    La biblioteca listaba toda raíz (`parent__isnull`), no solo las especialidades, así que el
    bucket habría aparecido como una más. Se filtra por `is_specialty_root`, igual que ya hace la
    reconciliación, y las fichas se navegan en su propia ventana.
    """
    from library.models import FICHA_BUCKET_NAME, Collection

    Collection.ficha_bucket()
    Collection.objects.create(name="Cardiología", is_specialty_root=True)

    resp = client.get(reverse("ui:library"))

    nombres = [c.name for c in resp.context["specialties"]]
    assert "Cardiología" in nombres
    assert FICHA_BUCKET_NAME not in nombres


def _ficha(
    sha: str,
    *,
    title: str = "",
    active_ingredient: str = "",
    atc_groups: list[str] | None = None,
    status: str = Status.JSON_READY,
    con_registro: bool = True,
) -> SourceDocument:
    doc = SourceDocument(
        sha256=sha,
        doc_type=DocType.FICHA_TECNICA,
        status=status,
        title=title,
        active_ingredient=active_ingredient,
        atc_groups=atc_groups or [],
    )
    doc.pdf_file.save(f"{sha}.pdf", SimpleUploadedFile("x.pdf", b"%PDF-1.4"), save=True)
    if con_registro:
        CimaRegistration.objects.create(
            nregistro=sha[:12], document=doc, content_hash=sha[:16], es_canonico=True
        )
    return doc


@pytest.mark.django_db
def test_la_ventana_de_fichas_lista_solo_fichas(client: Client) -> None:
    """Un paper existe en la base pero no entra en la ventana de fichas técnicas."""
    _ficha("f1" + "0" * 62, title="DARZALEX 20 MG/ML", active_ingredient="DARATUMUMAB")
    SourceDocument.objects.create(sha256="e1" + "0" * 62, doc_type=DocType.PAPER, title="Un paper")

    resp = client.get(reverse("ui:fichas"))

    cuerpo = resp.content.decode()
    assert "DARZALEX 20 MG/ML" in cuerpo
    assert "Un paper" not in cuerpo


@pytest.mark.django_db
def test_excluye_la_ficha_subida_que_no_viene_de_cima(client: Client) -> None:
    """Un PDF de ficha subido queda FAILED sin CimaRegistration: no es lo que promete la ventana."""
    _ficha("f1" + "0" * 62, title="DARZALEX", active_ingredient="DARATUMUMAB")
    _ficha(
        "f2" + "0" * 62,
        title="FICHA SUBIDA A MANO",
        status=Status.FAILED,
        con_registro=False,
    )

    resp = client.get(reverse("ui:fichas"))

    cuerpo = resp.content.decode()
    assert "DARZALEX" in cuerpo
    assert "FICHA SUBIDA A MANO" not in cuerpo


@pytest.mark.django_db
def test_los_recuentos_de_faceta_siguen_la_busqueda(client: Client) -> None:
    """El número de cada grupo ATC cuenta lo que casa la búsqueda, no el total."""
    _ficha("f1" + "0" * 62, title="DARZALEX", active_ingredient="DARATUMUMAB", atc_groups=["L"])
    _ficha("f2" + "0" * 62, title="ADIRO", active_ingredient="AAS", atc_groups=["B"])

    resp = client.get(reverse("ui:fichas"), {"q": "daratumumab"})

    grupos = {g["code"]: g["n"] for g in resp.context["grupos"]}
    assert grupos == {"L": 1}


@pytest.mark.django_db
def test_busca_por_principio_activo_no_solo_por_el_nombre(client: Client) -> None:
    """El nombre es la marca («DARZALEX…»); «daratumumab» solo casa por el principio activo."""
    _ficha("f1" + "0" * 62, title="DARZALEX 20 MG/ML", active_ingredient="DARATUMUMAB")
    _ficha("f2" + "0" * 62, title="EUTIROX 50 MCG", active_ingredient="LEVOTIROXINA")

    resp = client.get(reverse("ui:fichas"), {"q": "daratumumab"})

    cuerpo = resp.content.decode()
    assert "DARZALEX" in cuerpo
    assert "EUTIROX" not in cuerpo


@pytest.mark.django_db
def test_busca_por_numero_de_registro(client: Client) -> None:
    doc = _ficha(
        "f1" + "0" * 62, title="DARZALEX", active_ingredient="DARATUMUMAB", con_registro=False
    )
    CimaRegistration.objects.create(
        nregistro="1161101001", document=doc, content_hash="h", es_canonico=True
    )
    _ficha("f2" + "0" * 62, title="EUTIROX", active_ingredient="LEVOTIROXINA")

    resp = client.get(reverse("ui:fichas"), {"q": "1161101001"})

    cuerpo = resp.content.decode()
    assert "DARZALEX" in cuerpo
    assert "EUTIROX" not in cuerpo


@pytest.mark.django_db
def test_filtra_por_grupo_atc_con_recuento(client: Client) -> None:
    _ficha("f1" + "0" * 62, title="DARZALEX", active_ingredient="DARATUMUMAB", atc_groups=["L"])
    _ficha("f2" + "0" * 62, title="ADIRO", active_ingredient="AAS", atc_groups=["B"])

    resp = client.get(reverse("ui:fichas"), {"atc": "L"})

    cuerpo = resp.content.decode()
    assert "DARZALEX" in cuerpo
    assert "ADIRO" not in cuerpo
    grupos = {g["code"]: g["n"] for g in resp.context["grupos"]}
    assert grupos == {"L": 1, "B": 1}


@pytest.mark.django_db
def test_una_ficha_por_documento_aunque_tenga_varios_registros(client: Client) -> None:
    """Los alias son varios `nregistro` del mismo documento: una sola fila, no una por registro."""
    doc = _ficha(
        "f1" + "0" * 62, title="DARZALEX", active_ingredient="DARATUMUMAB", con_registro=False
    )
    for nreg in ("1161101001", "1161101002"):
        CimaRegistration.objects.create(
            nregistro=nreg, document=doc, content_hash="h", es_canonico=(nreg.endswith("1"))
        )

    resp = client.get(reverse("ui:fichas"))

    assert len(resp.context["filas"]) == 1
    assert resp.context["filas"][0]["registros"] == ["1161101001", "1161101002"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("estado_capitulo", "se_encola"), [(Status.PARSING, False), (Status.INDEXED, True)]
)
def test_una_madre_con_capitulos_en_curso_no_se_regenera(
    client: Client, monkeypatch: pytest.MonkeyPatch, estado_capitulo: str, se_encola: bool
) -> None:
    from pipeline import tasks as pipe_tasks

    madre = _doc("m" * 64, status=Status.SPLIT)
    SourceDocument.objects.create(sha256="c" * 64, parent=madre, status=estado_capitulo)
    encolados: list[str] = []
    monkeypatch.setattr(pipe_tasks.process_document_task, "delay", lambda i: encolados.append(i))

    client.post(reverse("ui:process", args=[madre.pk]))

    assert encolados == ([str(madre.pk)] if se_encola else [])
    madre.refresh_from_db()
    assert madre.status == (Status.UPLOADED if se_encola else Status.SPLIT)
