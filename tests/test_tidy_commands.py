import contextlib
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.helpers import activate_test_instance
from pupu.instance_context import (
    InstanceContext,
    activate_instance_context,
    activate_instance_context_global,
    clear_instance_context_global,
    get_current_instance_context,
)

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"

from pupu import cli


class TidyCommandTests(unittest.TestCase):
    def test_cli_instance_selector_applies_selected_instance_env(self):
        keys = ("PUPU_REPO_ROOT",)
        old_values = {key: os.environ.get(key) for key in keys}
        self.addCleanup(self._restore_env, old_values)
        previous_context = get_current_instance_context()
        clear_instance_context_global()
        self.addCleanup(
            lambda: activate_instance_context_global(previous_context)
            if previous_context is not None
            else clear_instance_context_global()
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["PUPU_REPO_ROOT"] = str(root)
            for instance_id, display in (("b-inst", "Beta"), ("a-inst", "Alpha")):
                inst = root / "instances" / instance_id
                (inst / "data").mkdir(parents=True)
                (inst / "instance.json").write_text(
                    json.dumps(
                        {
                            "id": instance_id,
                            "display_name": display,
                            "qq_mode": "cli",
                            "port": 9000,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                (inst / "persona.json").write_text("{}", encoding="utf-8")

            with patch.object(cli.console, "print"):
                with patch.object(cli.console, "input", return_value="2"):
                    selected = cli._configure_cli_instance_interactively()

            expected = root / "instances" / "b-inst"
            self.assertEqual(selected, "b-inst")
            ctx = get_current_instance_context()
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.instance_dir, expected.resolve())
            self.assertEqual(ctx.db_path, expected.resolve() / "data" / "pupu.db")
            self.assertEqual(ctx.persona_path, expected.resolve() / "persona.json")

    def test_cli_instance_selector_skips_when_context_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "instances" / "selected"
            inst.mkdir(parents=True)
            (inst / "data").mkdir()
            (inst / "instance.json").write_text('{"display_name":"Selected"}', encoding="utf-8")

            with patch.object(cli.console, "input") as mock_input:
                with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                    selected = cli._configure_cli_instance_interactively()

            self.assertIsNone(selected)
            mock_input.assert_not_called()

    def test_cli_tidy_accepts_check_apply_rebuild_and_defaults_to_apply(self):
        cases = [
            ("/tidy", "apply"),
            ("/tidy check", "check"),
            ("/cleanup apply", "apply"),
            ("/tidy rebuild", "rebuild"),
        ]
        for command, mode in cases:
            with self.subTest(command=command):
                with patch.object(cli.console, "status", return_value=contextlib.nullcontext()) as mock_status:
                    with patch.object(cli.console, "print") as mock_print:
                        with patch("pupu.cli.run_semantic_maintenance", return_value=f"{mode} report") as mock_run:
                            handled = cli.handle_command(command)

                self.assertFalse(handled)
                mock_run.assert_called_once_with(cli.OWNER_SESSION, mode=mode)
                mock_status.assert_called_once()
                mock_print.assert_any_call(f"{mode} report")

    def test_cli_tidy_rejects_unknown_mode(self):
        with patch.object(cli.console, "print") as mock_print:
            with patch("pupu.cli.run_semantic_maintenance") as mock_run:
                handled = cli.handle_command("/tidy prune")

        self.assertFalse(handled)
        mock_run.assert_not_called()
        mock_print.assert_any_call(cli.TIDY_USAGE)

    def test_cli_proactive_switch_controls_env(self):
        cases = [
            ("/proactive off", False),
            ("/proactive on", True),
        ]
        for command, enabled in cases:
            with self.subTest(command=command):
                with patch.object(cli.console, "print"):
                    with patch("pupu.cli.set_proactive_enabled") as mock_set:
                        handled = cli.handle_command(command)

                self.assertFalse(handled)
                mock_set.assert_called_once_with(enabled)

    def test_cli_proactive_status_reports_switch(self):
        with patch.object(cli.console, "print") as mock_print:
            with patch("pupu.cli.is_proactive_enabled", return_value=False):
                handled = cli.handle_command("/proactive status")

        self.assertFalse(handled)
        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("关闭", printed)

    @staticmethod
    def _restore_env(old_values: dict[str, str | None]) -> None:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
