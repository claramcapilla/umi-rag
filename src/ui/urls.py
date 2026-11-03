from __future__ import annotations

from django.urls import path

from . import views

app_name = "ui"

urlpatterns = [
    path("", views.library, name="library"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/data/", views.dashboard_data, name="dashboard_data"),
    path("dashboard/retry/", views.retry_failed, name="retry_failed"),
    path("fidelity/", views.fidelity, name="fidelity"),
    path("fichas/", views.fichas, name="fichas"),
    path("cima/", views.cima, name="cima"),
    path("cima/ingest/", views.cima_ingest, name="cima_ingest"),
    path("cima/sync/", views.cima_sync, name="cima_sync"),
    path("cima/sync/visto/", views.cima_sync_visto, name="cima_sync_visto"),
    path("snapshots/", views.timeline, name="timeline"),
    path("snapshots/create/", views.create_snapshot_view, name="create_snapshot"),
    path("snapshots/compare/", views.snapshot_compare, name="snapshot_compare"),
    path("snapshots/<uuid:pk>/", views.snapshot_detail, name="snapshot_detail"),
    path("collections/create/", views.create_collection, name="create_collection"),
    path("upload/", views.upload, name="upload"),
    path("upload/progress/", views.upload_progress, name="upload_progress"),
    path("doc/<uuid:pk>/", views.document_detail, name="detail"),
    path("doc/<uuid:pk>/pdf/", views.serve_pdf, name="pdf"),
    path("doc/<uuid:pk>/json/", views.serve_json, name="json"),
    path("doc/<uuid:pk>/md/", views.serve_md, name="md"),
    path("doc/<uuid:pk>/progress/", views.doc_progress, name="doc_progress"),
    path("doc/<uuid:pk>/process/", views.process, name="process"),
    path("doc/<uuid:pk>/index/", views.index, name="index"),
    path("doc/<uuid:pk>/enrich/", views.enrich, name="enrich"),
    path("doc/<uuid:pk>/tags/add/", views.add_tag, name="add_tag"),
    path("doc/<uuid:pk>/tags/<uuid:tag_pk>/remove/", views.remove_tag, name="remove_tag"),
]
