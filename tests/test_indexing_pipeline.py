import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion.index_document import index_clinical_document
from indexing.pipeline import index_chunks
from indexing.qdrant_store import point_id_for_chunk


class ClinicalDocumentIndexingTests(unittest.TestCase):
    @patch(
        "ingestion.index_document.index_chunks",
        return_value={
            "qdrant_ids": ["point-1", "point-2"],
            "updated_count": 2,
            "deleted_count": 1,
            "embedding_model": "test-model",
            "collection_name": "documents",
        },
    )
    @patch(
        "ingestion.index_document.chunk_clinical_document",
        return_value=[
            {"chunk_id": "chunk-1", "text": "uno"},
            {"chunk_id": "chunk-2", "text": "dos"},
        ],
    )
    def test_expands_chunks_by_specialty_and_uses_shared_indexer(
        self,
        _chunk_clinical_document,
        shared_indexer,
    ):
        config_file = Path("custom-indexing.json")
        result = index_clinical_document(
            clinical_json={"title": "test"},
            document_id="document-1",
            document_type="paper",
            source_file="source.json",
            previous_ids=["old-point"],
            config_file=config_file,
            specialties=["neurology", "cardiology"],
        )

        _chunk_clinical_document.assert_called_once_with(
            clinical_json={"title": "test"},
            document_id="document-1",
            document_type="paper",
            source_file="source.json",
        )
        chunks = shared_indexer.call_args.args[0]
        self.assertEqual(
            [chunk["chunk_id"] for chunk in chunks],
            [
                "chunk-1__neurology",
                "chunk-2__neurology",
                "chunk-1__cardiology",
                "chunk-2__cardiology",
            ],
        )
        self.assertEqual(
            [chunk["specialty"] for chunk in chunks],
            ["neurology", "neurology", "cardiology", "cardiology"],
        )
        self.assertEqual(shared_indexer.call_args.kwargs["config_file"], config_file)
        self.assertEqual(
            shared_indexer.call_args.kwargs["previous_ids"],
            ["old-point"],
        )
        self.assertEqual(result["document_id"], "document-1")
        self.assertEqual(result["chunk_count"], 4)
        self.assertEqual(result["qdrant_ids"], ["point-1", "point-2"])


class IndexingPipelineTests(unittest.TestCase):
    @patch("indexing.pipeline.delete_points")
    @patch("indexing.pipeline.upsert_chunks", return_value=2)
    @patch("indexing.pipeline.ensure_collection")
    @patch("indexing.pipeline.create_client")
    @patch(
        "indexing.pipeline.embed_chunks_with_cache",
        return_value=[[1.0], [2.0]],
    )
    @patch("indexing.pipeline.create_embedder")
    @patch("indexing.pipeline.resolve_worker_config", return_value=(1, 1, []))
    @patch("indexing.pipeline.load_config")
    def test_indexes_chunks_and_removes_stale_points(
        self,
        load_config,
        _resolve_worker_config,
        _create_embedder,
        _embed_chunks,
        create_client,
        ensure_collection,
        upsert_chunks,
        delete_points,
    ):
        client = create_client.return_value
        load_config.return_value = {
            "embeddings": {"model_name": "test-model", "document_prefix": ""},
            "indexing": {
                "batch_size": 5,
                "embedding_batch_size": 2,
                "cache_batch_size": 3,
                "use_cache": True,
            },
            "paths": {"embedding_cache_dir": "cache"},
            "qdrant": {"collection_name": "documents"},
        }
        chunks = [
            {"chunk_id": "chunk-1", "text": "uno"},
            {"chunk_id": "chunk-2", "text": "dos"},
        ]
        current_ids = [point_id_for_chunk(chunk["chunk_id"]) for chunk in chunks]
        stale_id = "stale-point"

        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "configs" / "indexing_config.json"
            result = index_chunks(
                chunks,
                config_file=config_file,
                previous_ids=[stale_id, current_ids[0]],
            )

        self.assertEqual(result["qdrant_ids"], current_ids)
        self.assertEqual(result["updated_count"], 2)
        self.assertEqual(result["deleted_count"], 1)
        _resolve_worker_config.assert_called_once()
        _create_embedder.assert_called_once()
        _embed_chunks.assert_called_once()
        upsert_chunks.assert_called_once()
        delete_points.assert_called_once_with(client, "documents", [stale_id])
        client.close.assert_called_once()
        ensure_collection.assert_called_once_with(client, "documents", 1)


if __name__ == "__main__":
    unittest.main()
