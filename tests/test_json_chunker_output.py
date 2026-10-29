import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from chunking.chunkers.json import HierarchicalJSONChunker, DOCUMENT_PLAN_PAPER
from indexing.normalizer import iter_normalized_chunks


class JsonChunkerOutputTests(unittest.TestCase):
    def test_hierarchical_chunks_are_accepted_by_indexing_normalizer(self):
        source_data = {
            "article_type": "research",
            "title": "A clinical study",
            "abstract": "A short clinical summary.",
        }
        chunker = HierarchicalJSONChunker(
            target_tokens=20,
            max_tokens=50,
            token_counter=lambda text: len(text.split()),
            document_plan=DOCUMENT_PLAN_PAPER,
        )
        chunks = chunker.chunk_document(source_data)

        with tempfile.TemporaryDirectory() as temp_dir:
            chunks_dir = Path(temp_dir) / "json" / "chunk_results"
            chunks_dir.mkdir(parents=True)
            result_path = chunks_dir / "JC_study.json"
            result_path.write_text(
                json.dumps(
                    {
                        "strategy": "JSONChunking",
                        "type": "HierarchicalJSONChunker",
                        "abbreviation": "JC",
                        "doc_id": "study",
                        "document_type": "paper",
                        "input_file": "study.json",
                        "source_path": "study.json",
                        "chunks": [asdict(chunk) for chunk in chunks],
                    }
                ),
                encoding="utf-8",
            )

            normalized_chunks = list(
                iter_normalized_chunks(Path(temp_dir))
            )

        self.assertTrue(normalized_chunks)
        self.assertTrue(
            all(chunk["format"] == "json" for chunk in normalized_chunks)
        )
        self.assertTrue(
            all(
                chunk["strategy"] == "JSONChunking"
                for chunk in normalized_chunks
            )
        )
        self.assertTrue(
            all("block" in chunk["metadata"] for chunk in normalized_chunks)
        )


if __name__ == "__main__":
    unittest.main()
