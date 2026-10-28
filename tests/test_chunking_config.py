import json
import tempfile
import unittest
from pathlib import Path

from chunking.config import load_config, resolve_config_path


class ChunkingConfigTests(unittest.TestCase):
    def test_default_paths_resolve_from_project_root(self):
        project_root = Path(__file__).resolve().parents[1]
        config = load_config(project_root / "configs" / "chunker_config.json")

        self.assertEqual(
            resolve_config_path(
                project_root,
                config["paths"]["input_dir"],
                "paths.input_dir",
            ),
            project_root / "data" / "processed" / "json",
        )
        self.assertEqual(
            resolve_config_path(
                project_root,
                config["paths"]["output_dir"],
                "paths.output_dir",
            ),
            project_root / "data" / "processed" / "chunks",
        )

    def test_absolute_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            absolute_path = Path(temp_dir) / "external"

            self.assertEqual(
                resolve_config_path(
                    project_root,
                    str(absolute_path),
                    "paths.input_dir",
                ),
                absolute_path,
            )

    def test_invalid_path_setting_is_reported(self):
        with self.assertRaisesRegex(ValueError, "paths.input_dir"):
            resolve_config_path(Path("."), "", "paths.input_dir")

    def test_non_object_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps([]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "objeto JSON"):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
