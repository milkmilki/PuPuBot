import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.important_event_report import format_important_events_report
from pupu.memory import init_db, reset_session, upsert_important_events


class ImportantEventReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_important_event_report_{self._testMethodName}"
        reset_session(self.session_id)

    def test_report_includes_confidence_and_key(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "birthday-2026-04-27",
                    "title": "用户明天生日",
                    "kind": "birthday",
                    "event_time": "2026-04-27",
                    "time_text": "明天",
                    "details": "用户说明天过生日",
                    "followup_hint": "记得先祝她生日快乐",
                    "confidence": 0.95,
                    "status": "active",
                }
            ],
        )

        report = format_important_events_report(self.session_id)

        self.assertIn("重要事件 1 条", report)
        self.assertIn("confidence=0.95", report)
        self.assertIn("key=birthday-2026-04-27", report)
        self.assertIn("用户明天生日", report)


if __name__ == "__main__":
    unittest.main()
