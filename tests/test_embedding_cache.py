import tempfile
import unittest
from pathlib import Path

from umi_rag.indexing.embedding_cache import embed_chunks_with_cache


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(texts)
        return [[float(len(text)), 1.0] for text in texts]


class EmbeddingCacheTests(unittest.TestCase):
    def test_reuses_cached_vectors_and_recomputes_changed_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            embedder = FakeEmbedder()
            chunks = [
                {"chunk_id": "one", "text": "alpha"},
                {"chunk_id": "two", "text": "beta"},
            ]

            first_vectors = embed_chunks_with_cache(
                embedder, chunks, "test-model", cache_dir, "doc: ", 2, True
            )
            chunks[1]["text"] = "changed"
            second_vectors = embed_chunks_with_cache(
                embedder, chunks, "test-model", cache_dir, "doc: ", 2, True
            )

            self.assertEqual(len(embedder.calls), 2)
            self.assertEqual(embedder.calls[0], ["doc: alpha", "doc: beta"])
            self.assertEqual(embedder.calls[1], ["doc: changed"])
            self.assertEqual(first_vectors[0], second_vectors[0])
            self.assertNotEqual(first_vectors[1], second_vectors[1])


if __name__ == "__main__":
    unittest.main()
