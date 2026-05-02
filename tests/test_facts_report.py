import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.facts_report import format_facts_report
from pupu.memory import init_db, reset_session, upsert_self_facts, upsert_user_facts


class FactsReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_facts_report_{self._testMethodName}"
        reset_session(self.session_id)

    def test_empty_report(self):
        report = format_facts_report(self.session_id)

        self.assertEqual(report, "当前没有长期 facts 记忆。")

    def test_report_includes_user_and_self_facts(self):
        upsert_user_facts({"身份": "读研学生"}, self.session_id)
        upsert_self_facts({"自称": "姐姐"}, self.session_id)

        report = format_facts_report(self.session_id)

        self.assertIn("用户 facts 1 条", report)
        self.assertIn("1. 身份: 读研学生", report)
        self.assertIn("仆仆 self_facts 1 条", report)
        self.assertIn("1. 自称: 姐姐", report)


if __name__ == "__main__":
    unittest.main()
