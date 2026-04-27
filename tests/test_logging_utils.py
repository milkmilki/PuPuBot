import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pupu.logging_utils as logging_utils


class _FakeDatetime:
    current = datetime(2026, 4, 27, 23, 59, 0)

    @classmethod
    def now(cls):
        return cls.current


class RuntimeLoggingTests(unittest.TestCase):
    def test_log_file_rotates_when_date_changes(self):
        old_initialized = logging_utils._initialized
        old_log_file = logging_utils._log_file
        old_log_path = logging_utils._log_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                logging_utils._initialized = True
                logging_utils._log_file = None
                logging_utils._log_path = None

                with patch.object(logging_utils, "_get_project_root", return_value=root):
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
                self.assertIn("first", (root / "data" / "logs" / "pupu-20260427.log").read_text(encoding="utf-8"))
                self.assertIn("second", (root / "data" / "logs" / "pupu-20260428.log").read_text(encoding="utf-8"))
            finally:
                if logging_utils._log_file is not None and logging_utils._log_file is not old_log_file:
                    try:
                        logging_utils._log_file.close()
                    except Exception:
                        pass
                logging_utils._initialized = old_initialized
                logging_utils._log_file = old_log_file
                logging_utils._log_path = old_log_path


if __name__ == "__main__":
    unittest.main()
