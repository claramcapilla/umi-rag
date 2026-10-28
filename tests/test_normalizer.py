import json
import tempfile
import unittest
from pathlib import Path

from umi_rag.indexing.normalizer import iter_normalized_chunks, write_normalized_chunks


class NormalizerTests(unittest.TestCase):
    def test_normalizes_benchmark_chunk_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "chunks"
            result_dir = source / "fold" / "markdown" / "doc" / "chunk_results"
            result_dir.mkdir(parents=True)
            source_file = result_dir / "result.json"
            source_file.write_text(
                json.dumps(
                    {
                        "doc_id": "doc-1",
                        "strategy": "fixed",
                        "chunks": [{"text": "Texto", "chunk_id": "old-id"}],
                    }
                ),
                encoding="utf-8",
            )

            [chunk] = list(iter_normalized_chunks(source))

            self.assertEqual(chunk["text"], "Texto")
            self.assertEqual(chunk["format"], "markdown")
            self.assertEqual(chunk["document_id"], "doc-1")
            self.assertEqual(chunk["metadata"]["original_chunk_id"], "old-id")

    def test_normalizes_canonical_jsonl_and_writes_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "chunks"
            source.mkdir()
            (source / "chunks.jsonl").write_text(
                '{"chunk_id":"c-1","text":"Contenido"}\n',
                encoding="utf-8",
            )
            output = Path(temp_dir) / "normalized" / "chunks.jsonl"

            self.assertEqual(write_normalized_chunks(source, output), 1)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["chunk_id"],
                "c-1",
            )

    def test_missing_chunk_ids_are_stable_and_unique_across_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "chunks"
            (source / "first").mkdir(parents=True)
            (source / "second").mkdir()
            for folder in ("first", "second"):
                (source / folder / "chunks.jsonl").write_text(
                    '{"text":"Contenido"}\n',
                    encoding="utf-8",
                )

            first_run = list(iter_normalized_chunks(source))
            second_run = list(iter_normalized_chunks(source))

            ids = [chunk["chunk_id"] for chunk in first_run]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(ids, [chunk["chunk_id"] for chunk in second_run])


if __name__ == "__main__":
    unittest.main()
