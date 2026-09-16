import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pupu import cli
from pupu.instance_context import (
    InstanceContext,
    activate_instance_context,
    activate_instance_context_global,
    clear_instance_context_global,
    get_current_instance_context,
)


class CliInstanceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_context = get_current_instance_context()
        clear_instance_context_global()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PUPU_REPO_ROOT",
                "PUPU_YAML_PATH",
            )
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmpdir.name
        empty_yaml = Path(self._tmpdir.name) / "pupu.yaml"
        empty_yaml.write_text("", encoding="utf-8")
        os.environ["PUPU_YAML_PATH"] = str(empty_yaml)

    def tearDown(self) -> None:
        if self._previous_context is not None:
            activate_instance_context_global(self._previous_context)
        else:
            clear_instance_context_global()
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
        ctx = get_current_instance_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.instance_dir, inst.resolve())
        self.assertEqual(ctx.config_path, inst.resolve() / "instance.json")
        self.assertEqual(ctx.db_path, inst.resolve() / "data" / "pupu.db")
        self.assertEqual(ctx.persona_path, inst.resolve() / "persona.json")
        self.assertTrue((inst / "instance.json").is_file())
        self.assertTrue((inst / "persona.json").is_file())

    def test_direct_cli_selects_existing_instance(self) -> None:
        from pupu_console import instance_store

        instance_id = instance_store.create_instance("Existing", qq_mode="cli", port=8899)
        with patch.object(cli.console, "input", return_value="1"):
            selected = cli._configure_cli_instance_interactively()

        inst = Path(self._tmpdir.name) / "instances" / instance_id
        self.assertEqual(selected, instance_id)
        ctx = get_current_instance_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.instance_dir, inst.resolve())

    def test_active_instance_context_skips_prompt(self) -> None:
        inst = Path(self._tmpdir.name) / "instances" / "selected"
        inst.mkdir(parents=True)
        (inst / "data").mkdir()
        (inst / "instance.json").write_text('{"display_name":"Selected"}', encoding="utf-8")
        with patch.object(cli.console, "input") as input_mock:
            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                selected = cli._configure_cli_instance_interactively()
            self.assertIsNone(selected)
            input_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
