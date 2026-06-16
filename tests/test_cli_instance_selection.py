import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pupu import cli


class CliInstanceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PUPU_REPO_ROOT",
                "PUPU_INSTANCE_DIR",
                "PUPU_CONFIG_PATH",
                "PUPU_DB_PATH",
                "PUPU_MEMU_DB_PATH",
                "PUPU_PERSONA_PATH",
                "PUPU_YAML_PATH",
            )
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmpdir.name
        empty_yaml = Path(self._tmpdir.name) / "pupu.yaml"
        empty_yaml.write_text("", encoding="utf-8")
        os.environ["PUPU_YAML_PATH"] = str(empty_yaml)
        for key in self._old_env:
            if key not in {"PUPU_REPO_ROOT", "PUPU_YAML_PATH"}:
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_direct_cli_creates_instance_when_none_exists(self) -> None:
        with patch.object(cli.console, "input", return_value="Test PuPu"):
            instance_id = cli._configure_cli_instance_interactively()

        self.assertTrue(instance_id)
        inst = Path(self._tmpdir.name) / "instances" / str(instance_id)
        self.assertEqual(os.environ["PUPU_INSTANCE_DIR"], str(inst.resolve()))
        self.assertEqual(os.environ["PUPU_CONFIG_PATH"], str(inst.resolve() / "instance.json"))
        self.assertEqual(os.environ["PUPU_DB_PATH"], str(inst.resolve() / "data" / "pupu.db"))
        self.assertEqual(os.environ["PUPU_MEMU_DB_PATH"], str(inst.resolve() / "data" / "memu.db"))
        self.assertEqual(os.environ["PUPU_PERSONA_PATH"], str(inst.resolve() / "persona.json"))
        self.assertTrue((inst / "instance.json").is_file())
        self.assertTrue((inst / "persona.json").is_file())

    def test_direct_cli_selects_existing_instance(self) -> None:
        from pupu_console import instance_store

        instance_id = instance_store.create_instance("Existing", qq_mode="cli", port=8899)
        with patch.object(cli.console, "input", return_value="1"):
            selected = cli._configure_cli_instance_interactively()

        inst = Path(self._tmpdir.name) / "instances" / instance_id
        self.assertEqual(selected, instance_id)
        self.assertEqual(os.environ["PUPU_INSTANCE_DIR"], str(inst.resolve()))

    def test_configured_instance_env_skips_prompt(self) -> None:
        os.environ["PUPU_INSTANCE_DIR"] = "already-selected"
        with patch.object(cli.console, "input") as input_mock:
            selected = cli._configure_cli_instance_interactively()
        self.assertIsNone(selected)
        input_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
