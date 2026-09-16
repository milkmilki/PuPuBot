import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pupu.maintenance.error_log import (
    ERROR_LOG_RETENTION_DAYS,
    RAW_RESPONSE_MAX_CHARS,
    record_model_response_error,
)


class MaintenanceErrorLogTests(unittest.TestCase):
    def test_records_raw_response_and_diagnostic_metadata(self):
        raw_response = '{"actions": [broken]}'
        with tempfile.TemporaryDirectory() as tmp:
            path = record_model_response_error(
                task_name="facts_maintenance",
                provider="deepseek:test",
                raw_response=raw_response,
                error=ValueError("invalid JSON"),
                now=datetime(2026, 4, 26, 3, 0, 0),
                log_dir=Path(tmp),
            )

            record = json.loads(path.read_text(encoding="utf-8"))
            file_mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(path.name, "maintenance-model-errors-20260426.jsonl")
        self.assertEqual(record["task_name"], "facts_maintenance")
        self.assertEqual(record["provider"], "deepseek:test")
        self.assertEqual(record["error_type"], "ValueError")
        self.assertEqual(record["raw_response"], raw_response)
        self.assertEqual(record["raw_response_chars"], len(raw_response))
        self.assertEqual(
            record["raw_response_sha256"],
            hashlib.sha256(raw_response.encode("utf-8")).hexdigest(),
        )
        self.assertFalse(record["truncated"])
        if os.name != "nt":
            self.assertEqual(file_mode, 0o600)

    def test_caps_raw_response_and_prunes_old_daily_logs(self):
        raw_response = "x" * (RAW_RESPONSE_MAX_CHARS + 17)
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            for day in range(1, ERROR_LOG_RETENTION_DAYS + 2):
                log_path = (
                    log_dir / f"maintenance-model-errors-202604{day:02d}.jsonl"
                )
                log_path.write_text(
                    "{}\n",
                    encoding="utf-8",
                )

            path = record_model_response_error(
                task_name="event_thread_maintenance",
                provider="anthropic:test",
                raw_response=raw_response,
                error=ValueError("invalid JSON"),
                now=datetime(2026, 4, 30, 3, 0, 0),
                log_dir=log_dir,
            )
            record = json.loads(path.read_text(encoding="utf-8"))
            retained = sorted(log_dir.glob("maintenance-model-errors-*.jsonl"))

        self.assertEqual(len(record["raw_response"]), RAW_RESPONSE_MAX_CHARS)
        self.assertEqual(record["raw_response_chars"], len(raw_response))
        self.assertTrue(record["truncated"])
        self.assertEqual(len(retained), ERROR_LOG_RETENTION_DAYS)
        self.assertNotIn(
            "maintenance-model-errors-20260401.jsonl",
            {p.name for p in retained},
        )


if __name__ == "__main__":
    unittest.main()
