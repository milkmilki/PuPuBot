import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

import nonebot

nonebot.init()

from plugins.pupu_support import commands as nb_commands
from pupu import cli


class TidyCommandTests(unittest.IsolatedAsyncioTestCase):
    def _owner_event(self) -> SimpleNamespace:
        return SimpleNamespace(get_user_id=lambda: "owner")

    def test_cli_tidy_accepts_check_apply_and_defaults_to_apply(self):
        cases = [
            ("/tidy", "apply"),
            ("/tidy check", "check"),
            ("/cleanup apply", "apply"),
        ]
        for command, mode in cases:
            with self.subTest(command=command):
                with patch.object(cli.console, "status", return_value=contextlib.nullcontext()) as mock_status:
                    with patch.object(cli.console, "print") as mock_print:
                        with patch("pupu.cli.run_memu_maintenance", return_value=f"{mode} report") as mock_run:
                            handled = cli.handle_command(command)

                self.assertFalse(handled)
                mock_run.assert_called_once_with(cli.OWNER_SESSION, mode=mode)
                mock_status.assert_called_once()
                mock_print.assert_any_call(f"{mode} report")

    def test_cli_tidy_rejects_unknown_mode(self):
        with patch.object(cli.console, "print") as mock_print:
            with patch("pupu.cli.run_memu_maintenance") as mock_run:
                handled = cli.handle_command("/tidy prune")

        self.assertFalse(handled)
        mock_run.assert_not_called()
        mock_print.assert_any_call(cli.TIDY_USAGE)

    async def test_nonebot_tidy_defaults_to_apply_and_accepts_check(self):
        cases = [
            ("", "apply"),
            ("check", "check"),
        ]
        for arg, mode in cases:
            with self.subTest(arg=arg):
                args = SimpleNamespace(extract_plain_text=lambda: arg)
                with patch("plugins.pupu_support.commands.is_owner", return_value=True):
                    with patch(
                        "plugins.pupu_support.commands.run_memu_maintenance",
                        return_value=f"{mode} report",
                    ) as mock_run:
                        with patch.object(nb_commands.tidy_cmd, "finish", new=AsyncMock()) as mock_finish:
                            await nb_commands.handle_tidy(self._owner_event(), args=args)

                mock_run.assert_called_once_with("owner", mode=mode)
                mock_finish.assert_awaited_once_with(f"{mode} report")

    async def test_nonebot_tidy_rejects_unknown_mode(self):
        args = SimpleNamespace(extract_plain_text=lambda: "wipe")
        with patch("plugins.pupu_support.commands.is_owner", return_value=True):
            with patch("plugins.pupu_support.commands.run_memu_maintenance") as mock_run:
                with patch.object(nb_commands.tidy_cmd, "finish", new=AsyncMock()) as mock_finish:
                    await nb_commands.handle_tidy(self._owner_event(), args=args)

        mock_run.assert_not_called()
        mock_finish.assert_awaited_once_with(nb_commands.TIDY_USAGE)


if __name__ == "__main__":
    unittest.main()
