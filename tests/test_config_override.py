import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class ConfigPathOverrideTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("PUPU_CONFIG_PATH", None)
        import pupu.config as cfg

        importlib.reload(cfg)

    def test_load_config_uses_pupu_config_path(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump({"owner_ids": ["42"]}, f)
            path = f.name
        try:
            os.environ["PUPU_CONFIG_PATH"] = path
            import pupu.config as cfg

            importlib.reload(cfg)
            self.assertEqual(cfg.get_config_path(), Path(path))
            self.assertEqual(cfg.load_owner_ids(), ["42"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_owner_ids_defaults_when_key_missing(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump({"qq_mode": "napcat"}, f)
            path = f.name
        try:
            os.environ["PUPU_CONFIG_PATH"] = path
            import pupu.config as cfg

            importlib.reload(cfg)
            self.assertEqual(cfg.load_owner_ids(), [])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_load_owner_ids_respects_explicit_empty(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump({"owner_ids": []}, f)
            path = f.name
        try:
            os.environ["PUPU_CONFIG_PATH"] = path
            import pupu.config as cfg

            importlib.reload(cfg)
            self.assertEqual(cfg.load_owner_ids(), [])
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
