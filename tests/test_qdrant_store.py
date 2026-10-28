import unittest
from types import SimpleNamespace

from indexing.qdrant_store import upsert_chunks


class InMemoryQdrant:
    def __init__(self):
        self.points = {}
        self.upsert_calls = []

    def retrieve(self, collection_name, ids, with_payload, with_vectors):
        return [
            SimpleNamespace(id=point_id, payload=self.points[point_id].payload)
            for point_id in ids
            if point_id in self.points
        ]

    def upsert(self, collection_name, points):
        self.upsert_calls.append(points)
        for point in points:
            self.points[str(point.id)] = point


class IncrementalUpsertTests(unittest.TestCase):
    def test_only_new_or_changed_chunks_are_written(self):
        client = InMemoryQdrant()
        first_chunks = [
            {"chunk_id": "chunk-1", "text": "uno"},
            {"chunk_id": "chunk-2", "text": "dos"},
        ]
        vectors = [[1.0, 0.0], [0.0, 1.0]]

        first_count = upsert_chunks(
            client, "collection", first_chunks, vectors, 10, "model-v1"
        )
        repeat_count = upsert_chunks(
            client, "collection", first_chunks, vectors, 10, "model-v1"
        )
        updated_chunks = [
            first_chunks[0],
            {"chunk_id": "chunk-2", "text": "actualizado"},
            {"chunk_id": "chunk-3", "text": "nuevo"},
        ]
        updated_count = upsert_chunks(
            client,
            "collection",
            updated_chunks,
            vectors + [[0.5, 0.5]],
            10,
            "model-v1",
        )

        self.assertEqual(first_count, 2)
        self.assertEqual(repeat_count, 0)
        self.assertEqual(updated_count, 2)
        self.assertEqual(len(client.points), 3)
        self.assertEqual(len(client.upsert_calls), 2)


if __name__ == "__main__":
    unittest.main()
