import json
import tempfile
import unittest
from pathlib import Path

from pupu.instance_context import InstanceContext, activate_instance_context


class ConfigPathOverrideTests(unittest.TestCase):
    def _write_instance_config(self, root: Path, payload: dict) -> Path:
        inst = root / "instances" / "abc123"
        (inst / "data").mkdir(parents=True)
        (inst / "instance.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return inst

    def test_load_config_uses_active_instance_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._write_instance_config(Path(tmp), {"owner_ids": ["42"]})
            import pupu.config as cfg

            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(cfg.get_config_path(), inst.resolve() / "instance.json")
                self.assertEqual(cfg.load_owner_ids(), ["42"])

    def test_load_owner_ids_defaults_when_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._write_instance_config(Path(tmp), {"qq_mode": "napcat"})
            import pupu.config as cfg

            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(cfg.load_owner_ids(), [])

    def test_load_owner_ids_respects_explicit_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._write_instance_config(Path(tmp), {"owner_ids": []})
            import pupu.config as cfg

            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(cfg.load_owner_ids(), [])

    def test_private_reply_allowlist_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._write_instance_config(
                Path(tmp),
                {
                    "owner_ids": ["42"],
                    "private_reply_mode": "allowlist",
                    "private_allowed_ids": ["7", "8"],
                },
            )
            import pupu.config as cfg

            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(cfg.load_private_reply_mode(), "allowlist")
                self.assertEqual(cfg.load_private_allowed_ids(), ["7", "8"])
                self.assertTrue(cfg.is_private_reply_allowed("42"))
                self.assertTrue(cfg.is_private_reply_allowed("7"))
                self.assertFalse(cfg.is_private_reply_allowed("9"))

    def test_private_reply_defaults_to_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._write_instance_config(Path(tmp), {"owner_ids": ["42"]})
            import pupu.config as cfg

            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(cfg.load_private_reply_mode(), "owner_only")
                self.assertTrue(cfg.is_private_reply_allowed("42"))
                self.assertFalse(cfg.is_private_reply_allowed("7"))


if __name__ == "__main__":
    unittest.main()
