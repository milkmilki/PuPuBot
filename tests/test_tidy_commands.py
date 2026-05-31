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


class _FakeTask:
    def __init__(self, done: bool = False):
        self._done = done
        self.cancelled = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True


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

    def test_cli_proactive_switch_controls_env(self):
        cases = [
            ("/proactive off", False, "主动消息已关闭。"),
            ("/proactive on", True, "主动消息已开启。QQ 后台循环会在连接后运行；CLI 里只保存开关。"),
        ]
        for command, enabled, message in cases:
            with self.subTest(command=command):
                with patch.object(cli.console, "print") as mock_print:
                    with patch("pupu.cli.set_proactive_enabled") as mock_set:
                        handled = cli.handle_command(command)

                self.assertFalse(handled)
                mock_set.assert_called_once_with(enabled)
                mock_print.assert_any_call(message)

    def test_cli_proactive_status_reports_switch(self):
        with patch.object(cli.console, "print") as mock_print:
            with patch("pupu.cli.is_proactive_enabled", return_value=False):
                handled = cli.handle_command("/proactive status")

        self.assertFalse(handled)
        mock_print.assert_any_call("主动消息：已关闭")

    async def test_nonebot_proactive_off_cancels_loop(self):
        args = SimpleNamespace(extract_plain_text=lambda: "off")
        task = _FakeTask()
        nb_commands.state.proactive_task = task
        try:
            with patch("plugins.pupu_support.commands.is_owner", return_value=True):
                with patch("plugins.pupu_support.commands.set_proactive_enabled") as mock_set:
                    with patch.object(nb_commands.proactive_cmd, "finish", new=AsyncMock()) as mock_finish:
                        await nb_commands.handle_proactive(self._owner_event(), args=args, bot=None)
        finally:
            nb_commands.state.proactive_task = None

        self.assertTrue(task.cancelled)
        mock_set.assert_called_once_with(False)
        mock_finish.assert_awaited_once_with("主动消息已关闭。")

    async def test_nonebot_proactive_status_reports_switch(self):
        args = SimpleNamespace(extract_plain_text=lambda: "status")
        try:
            nb_commands.state.proactive_task = None
            with patch("plugins.pupu_support.commands.is_owner", return_value=True):
                with patch("plugins.pupu_support.commands.is_proactive_enabled", return_value=False):
                    with patch.object(nb_commands.proactive_cmd, "finish", new=AsyncMock()) as mock_finish:
                        await nb_commands.handle_proactive(self._owner_event(), args=args, bot=None)
        finally:
            nb_commands.state.proactive_task = None

        text = mock_finish.await_args.args[0]
        self.assertIn("主动消息：已关闭", text)
        self.assertIn("后台循环：未运行", text)

    async def test_nonebot_proactive_on_persists_and_starts_loop(self):
        args = SimpleNamespace(extract_plain_text=lambda: "on")
        with patch("plugins.pupu_support.commands.is_owner", return_value=True):
            with patch("plugins.pupu_support.commands.set_proactive_enabled") as mock_set:
                with patch(
                    "plugins.pupu_support.commands._start_proactive_loop_from_command",
                    new=AsyncMock(return_value="started"),
                ) as mock_start:
                    with patch.object(nb_commands.proactive_cmd, "finish", new=AsyncMock()) as mock_finish:
                        await nb_commands.handle_proactive(self._owner_event(), args=args, bot="bot")

        mock_set.assert_called_once_with(True)
        mock_start.assert_awaited_once_with("bot")
        mock_finish.assert_awaited_once_with("started")


if __name__ == "__main__":
    unittest.main()
