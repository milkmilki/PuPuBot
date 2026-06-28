import tempfile
import unittest
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pupu.logging_utils as logging_utils
from pupu.instance_context import InstanceContext, activate_instance_context


class _FakeDatetime:
    current = datetime(2026, 4, 27, 23, 59, 0)

    @classmethod
    def now(cls):
        return cls.current


class RuntimeLoggingTests(unittest.TestCase):
    def tearDown(self):
        logging_utils.set_debug_console_enabled(False)

    def test_log_file_rotates_when_date_changes(self):
        old_initialized = logging_utils._initialized
        old_log_file = logging_utils._log_file
        old_log_path = logging_utils._log_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = root / "instances" / "logtest"
            (inst / "data").mkdir(parents=True)
            (inst / "instance.json").write_text('{"display_name":"Log Test"}', encoding="utf-8")
            ctx = InstanceContext.from_instance_dir(inst)
            try:
                logging_utils._initialized = True
                logging_utils._log_file = None
                logging_utils._log_path = None

                with activate_instance_context(ctx):
                    with patch.object(logging_utils, "datetime", _FakeDatetime):
                        first_sink = logging_utils._ensure_current_log_file()
                        first_sink.write("first\n")
                        first_sink.flush()

                        _FakeDatetime.current = datetime(2026, 4, 28, 0, 0, 1)
                        second_sink = logging_utils._ensure_current_log_file()
                        second_sink.write("second\n")
                        second_sink.flush()

                self.assertIsNot(first_sink, second_sink)
                self.assertTrue(first_sink.closed)
                second_sink.close()
                self.assertIn("first", (inst / "data" / "logs" / "pupu-20260427.log").read_text(encoding="utf-8"))
                self.assertIn("second", (inst / "data" / "logs" / "pupu-20260428.log").read_text(encoding="utf-8"))
            finally:
                if logging_utils._log_file is not None and logging_utils._log_file is not old_log_file:
                    try:
                        logging_utils._log_file.close()
                    except Exception:
                        pass
                logging_utils._initialized = old_initialized
                logging_utils._log_file = old_log_file
                logging_utils._log_path = old_log_path

    def test_prune_old_logs_keeps_latest_three_daily_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            for day in range(1, 6):
                (log_dir / f"pupu-2026040{day}.log").write_text(f"log-{day}", encoding="utf-8")
            unrelated = log_dir / "manual.log"
            unrelated.write_text("keep me", encoding="utf-8")

            deleted = logging_utils.prune_old_logs(log_dir=log_dir)

            self.assertEqual(
                {path.name for path in deleted},
                {"pupu-20260401.log", "pupu-20260402.log"},
            )
            self.assertEqual(
                {path.name for path in log_dir.glob("*.log")},
                {
                    "manual.log",
                    "pupu-20260403.log",
                    "pupu-20260404.log",
                    "pupu-20260405.log",
                },
            )
            self.assertTrue(unrelated.exists())

    def test_log_rotation_prunes_old_daily_files(self):
        old_initialized = logging_utils._initialized
        old_log_file = logging_utils._log_file
        old_log_path = logging_utils._log_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = root / "instances" / "logtest"
            (inst / "data").mkdir(parents=True)
            (inst / "instance.json").write_text('{"display_name":"Log Test"}', encoding="utf-8")
            ctx = InstanceContext.from_instance_dir(inst)
            log_dir = inst / "data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            for day in range(25, 29):
                (log_dir / f"pupu-202604{day}.log").write_text(f"log-{day}", encoding="utf-8")
            try:
                logging_utils._initialized = True
                logging_utils._log_file = None
                logging_utils._log_path = None

                with activate_instance_context(ctx):
                    with patch.object(logging_utils, "datetime", _FakeDatetime):
                        _FakeDatetime.current = datetime(2026, 4, 29, 0, 0, 1)
                        sink = logging_utils._ensure_current_log_file()
                        sink.write("latest\n")
                        sink.flush()

                sink.close()
                self.assertEqual(
                    {path.name for path in log_dir.glob("pupu-*.log")},
                    {
                        "pupu-20260427.log",
                        "pupu-20260428.log",
                        "pupu-20260429.log",
                    },
                )
            finally:
                if logging_utils._log_file is not None and logging_utils._log_file is not old_log_file:
                    try:
                        logging_utils._log_file.close()
                    except Exception:
                        pass
                logging_utils._initialized = old_initialized
                logging_utils._log_file = old_log_file
                logging_utils._log_path = old_log_path

    def test_verbose_pupu_lines_are_hidden_from_console_unless_debug_enabled(self):
        sink = StringIO()
        with patch.object(logging_utils, "_original_print") as original_print:
            with patch.object(logging_utils, "_get_sink", return_value=sink):
                logging_utils.set_debug_console_enabled(False)
                logging_utils._patched_print("[pupu][semantic] recall start")

                original_print.assert_not_called()
                self.assertIn("[pupu][semantic] recall start", sink.getvalue())

                logging_utils.set_debug_console_enabled(True)
                logging_utils._patched_print("[pupu][semantic] recall start")

                original_print.assert_called_once()

    def test_tool_lines_are_hidden_from_console_unless_debug_enabled(self):
        sink = StringIO()
        with patch.object(logging_utils, "_original_print") as original_print:
            with patch.object(logging_utils, "_get_sink", return_value=sink):
                logging_utils.set_debug_console_enabled(False)
                logging_utils._patched_print("[pupu][tool] session=owner call=mcp__tavily__tavily_search")

                original_print.assert_not_called()
                self.assertIn("[pupu][tool] session=owner call=mcp__tavily__tavily_search", sink.getvalue())

                logging_utils.set_debug_console_enabled(True)
                logging_utils._patched_print("[pupu][tool] session=owner call=mcp__tavily__tavily_search")

                original_print.assert_called_once()

    def test_non_verbose_lines_still_print_normally(self):
        with patch.object(logging_utils, "_original_print") as original_print:
            with patch.object(logging_utils, "_get_sink", return_value=StringIO()):
                logging_utils.set_debug_console_enabled(False)
                logging_utils._patched_print("[pupu] logging to path")

        original_print.assert_called_once()

    def test_unicode_console_encode_error_does_not_skip_log_sink(self):
        sink = StringIO()
        console_lines: list[str] = []

        def gbk_console_print(*args, **kwargs):
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            text = sep.join(str(arg) for arg in args) + end
            text.encode("gbk")
            console_lines.append(text)

        with patch.object(logging_utils, "_original_print", side_effect=gbk_console_print):
            with patch.object(logging_utils, "_get_sink", return_value=sink):
                logging_utils._patched_print("[23:19:51] <<< recv | private | owner | 🤫")

        self.assertEqual(sink.getvalue(), "[23:19:51] <<< recv | private | owner | 🤫\n")
        self.assertEqual(console_lines, ["[23:19:51] <<< recv | private | owner | \\U0001f92b\n"])


if __name__ == "__main__":
    unittest.main()
