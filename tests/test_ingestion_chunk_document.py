import unittest
from unittest.mock import patch

from ingestion.chunk_document import chunk_clinical_document


class IngestionChunkDocumentTests(unittest.TestCase):
    def test_document_context_is_included_in_each_ingested_chunk(self):
        documents = {
            "paper": (
                {
                    "article_type": "research",
                    "title": "A clinical study",
                    "doi": "10.1234/example",
                    "journal": "Clinical Journal",
                    "year": 2024,
                    "abstract": "A short clinical summary.",
                },
                {
                    "doi": "10.1234/example",
                    "title": "A clinical study",
                    "journal": "Clinical Journal",
                    "year": 2024,
                },
            ),
            "guideline": (
                {
                    "metadata": {
                        "title": "A clinical guideline",
                        "organization": "Health Organization",
                        "version": "2.1",
                    },
                    "conditions": ["A condition"],
                },
                {
                    "metadata.title": "A clinical guideline",
                    "metadata.organization": "Health Organization",
                    "metadata.version": "2.1",
                },
            ),
            "ficha_tecnica": (
                {
                    "medicine_name": "Example medicine",
                    "pharmaceutical_form": "tablet",
                    "active_ingredient": "Example ingredient",
                    "strength": "10 mg",
                    "indications": "A clinical indication.",
                },
                {
                    "medicine_name": "Example medicine",
                    "pharmaceutical_form": "tablet",
                    "active_ingredient": "Example ingredient",
                    "strength": "10 mg",
                },
            ),
        }

        with (
            patch(
                "ingestion.chunk_document.create_token_counter",
                return_value=lambda text: len(text.split()),
            ),
            patch("ingestion.chunk_document.create_tokenizer"),
            patch(
                "ingestion.chunk_document.load_config",
                return_value={
                    "tokenizer": {
                        "model_name": "test-tokenizer",
                        "target_tokens": 5,
                        "max_tokens": 20,
                        "add_special_tokens": True,
                    }
                },
            ),
        ):
            for document_type, (clinical_json, expected_context) in documents.items():
                with self.subTest(document_type=document_type):
                    chunks = chunk_clinical_document(
                        clinical_json=clinical_json,
                        document_id=f"document-{document_type}",
                        document_type=document_type,
                    )

                    self.assertTrue(chunks)
                    self.assertTrue(
                        all(
                            chunk["metadata"]["context"] == expected_context
                            for chunk in chunks
                        )
                    )


if __name__ == "__main__":
    unittest.main()
