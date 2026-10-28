import unittest
from unittest.mock import patch

from umi_rag.indexing.embedder import (
    create_embedder,
    iter_embedding_batches,
    resolve_worker_config,
)


class EmbedderTests(unittest.TestCase):
    @patch("umi_rag.indexing.embedder.HuggingFaceEmbeddings")
    def test_does_not_enable_remote_code_based_on_model_name(self, embedder_class):
        create_embedder(
            {
                "model_name": "jinaai/example",
                "device": "cpu",
                "normalize_embeddings": True,
            }
        )

        kwargs = embedder_class.call_args.kwargs
        self.assertFalse(kwargs["model_kwargs"]["trust_remote_code"])

    @patch("umi_rag.indexing.embedder.HuggingFaceEmbeddings")
    def test_remote_code_must_be_enabled_explicitly(self, embedder_class):
        create_embedder(
            {
                "model_name": "another-provider/example",
                "trust_remote_code": True,
            }
        )

        kwargs = embedder_class.call_args.kwargs
        self.assertTrue(kwargs["model_kwargs"]["trust_remote_code"])

    def test_cpu_worker_configuration_is_preserved(self):
        self.assertEqual(
            resolve_worker_config(
                {
                    "num_workers": 3,
                    "cpu_threads": 2,
                    "num_gpus": 0,
                    "gpu_ids": [],
                }
            ),
            (3, 2, []),
        )

    @patch("umi_rag.indexing.embedder.torch.cuda.device_count", return_value=2)
    @patch("umi_rag.indexing.embedder.torch.cuda.is_available", return_value=True)
    def test_gpu_worker_configuration_uses_configured_ids(
        self, _is_available, _device_count
    ):
        self.assertEqual(
            resolve_worker_config(
                {
                    "num_workers": 1,
                    "cpu_threads": 1,
                    "num_gpus": 1,
                    "gpu_ids": [1],
                }
            ),
            (1, 1, [1]),
        )

    def test_multiprocess_batches_share_a_worker_pool(self):
        class FakeModel:
            def __init__(self):
                self.started = 0
                self.stopped = 0

            def start_multi_process_pool(self, target_devices):
                self.started += 1
                self.target_devices = target_devices
                return object()

            def encode_multi_process(self, texts, _pool, batch_size):
                return [[3.0, 4.0] for _ in texts]

            def stop_multi_process_pool(self, _pool):
                self.stopped += 1

        class FakeEmbedder:
            encode_kwargs = {"normalize_embeddings": True}

            def __init__(self):
                self.client = FakeModel()

        embedder = FakeEmbedder()
        batches = list(
            iter_embedding_batches(
                embedder=embedder,
                texts=["one", "two", "three"],
                embedding_batch_size=1,
                cache_batch_size=2,
                num_workers=2,
                cpu_threads=1,
                gpu_ids=[],
            )
        )

        self.assertEqual([start for start, _ in batches], [0, 2])
        self.assertEqual(embedder.client.started, 1)
        self.assertEqual(embedder.client.stopped, 1)
        self.assertEqual(embedder.client.target_devices, ["cpu", "cpu"])
        self.assertAlmostEqual(batches[0][1][0][0], 0.6)


if __name__ == "__main__":
    unittest.main()
