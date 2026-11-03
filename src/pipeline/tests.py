import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from documents.models import SourceDocument, Status, Stage
from library.models import Collection
from pipeline.index import index_document


class IndexDocumentTest(TestCase):

    def test_indexador_simulado(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):

                # Crear un documento de prueba
                doc = SourceDocument.objects.create(
                    sha256="a" * 64,
                    original_filename="test.pdf",
                    title="Documento de prueba",
                    doc_type="paper",
                )

                # Asociarlo a una especialidad
                especialidad = Collection.objects.create(
                    name="Neurología Test",
                    is_specialty_root=True,
                )
                doc.collections.add(especialidad)

                # Guardar un JSON clínico de prueba
                clinical_json = {
                    "title": "Documento de prueba",
                    "article_type": "research",
                    "year": "2025",
                }

                doc.json_file.save(
                    "test.json",
                    ContentFile(json.dumps(clinical_json).encode("utf-8")),
                    save=True,
                )

                # Preparar el documento para indexación
                doc.transition_to(Status.PARSING)
                doc.transition_to(Status.PARSED)
                doc.transition_to(Status.JSON_READY)

                # Simular el indexador real
                with patch(
                    "ingestion.index_document.index_clinical_document",
                    return_value={
                        "qdrant_ids": ["id-1", "id-2"],
                        "chunk_count": 2,
                    },
                ) as mock_indexador:

                    index_document(str(doc.pk))

                # Comprobar el resultado
                doc.refresh_from_db()

                self.assertEqual(doc.status, Status.INDEXED)
                self.assertEqual(doc.chunk_count, 2)
                self.assertEqual(doc.qdrant_ids, ["id-1", "id-2"])

                # Comprobar los argumentos enviados
                mock_indexador.assert_called_once()

                kwargs = mock_indexador.call_args.kwargs

                self.assertEqual(kwargs["document_id"], str(doc.pk))
                self.assertEqual(kwargs["document_type"], "paper")
                self.assertEqual(kwargs["specialties"], ["neurología_test"])
                self.assertEqual(kwargs["previous_ids"], [])

    def test_error_indexacion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):

                doc = SourceDocument.objects.create(
                    sha256="b" * 64,
                    original_filename="test_error.pdf",
                    title="Documento con error",
                    doc_type="paper",
                )

                especialidad = Collection.objects.create(
                    name="Neurología Error Test",
                    is_specialty_root=True,
                )
                doc.collections.add(especialidad)

                doc.json_file.save(
                    "test_error.json",
                    ContentFile(
                        json.dumps({"title": "Documento con error"}).encode("utf-8")
                    ),
                    save=True,
                )

                doc.transition_to(Status.PARSING)
                doc.transition_to(Status.PARSED)
                doc.transition_to(Status.JSON_READY)

                with patch(
                    "ingestion.index_document.index_clinical_document",
                    side_effect=RuntimeError("Error simulado de Qdrant"),
                ):
                    index_document(str(doc.pk))

                doc.refresh_from_db()

                self.assertEqual(doc.status, Status.FAILED)
                self.assertEqual(doc.error_stage, Stage.INDEX)
                self.assertIn("Error simulado de Qdrant", doc.error_msg)
                self.assertEqual(doc.qdrant_ids, [])
                self.assertEqual(doc.chunk_count, 0)

    def test_reindexacion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):

                doc = SourceDocument.objects.create(
                    sha256="c" * 64,
                    original_filename="test_reindex.pdf",
                    title="Documento para reindexar",
                    doc_type="paper",
                )

                especialidad = Collection.objects.create(
                    name="Neurología Reindex Test",
                    is_specialty_root=True,
                )
                doc.collections.add(especialidad)

                doc.json_file.save(
                    "test_reindex.json",
                    ContentFile(
                        json.dumps({
                            "title": "Documento para reindexar",
                            "article_type": "research",
                        }).encode("utf-8")
                    ),
                    save=True,
                )

                # Simular un documento previamente indexado.
                doc.transition_to(Status.PARSING)
                doc.transition_to(Status.PARSED)
                doc.transition_to(Status.JSON_READY)
                doc.transition_to(Status.INDEXING)

                doc.qdrant_ids = ["id-antiguo-1", "id-antiguo-2"]
                doc.chunk_count = 2
                doc.transition_to(Status.INDEXED)

                # Simular la nueva indexación.
                with patch(
                    "ingestion.index_document.index_clinical_document",
                    return_value={
                        "qdrant_ids": ["id-nuevo-1", "id-nuevo-2", "id-nuevo-3"],
                        "chunk_count": 3,
                    },
                ) as mock_indexador:

                    index_document(str(doc.pk))

                doc.refresh_from_db()

                # Comprobar que se transmiten los IDs anteriores.
                argumentos = mock_indexador.call_args.kwargs

                self.assertEqual(
                    argumentos["previous_ids"],
                    ["id-antiguo-1", "id-antiguo-2"],
                )

                # Comprobar que Django registra los nuevos.
                self.assertEqual(doc.status, Status.INDEXED)
                self.assertEqual(doc.chunk_count, 3)
                self.assertEqual(
                    doc.qdrant_ids,
                    ["id-nuevo-1", "id-nuevo-2", "id-nuevo-3"],
                )