import os
from datetime import datetime
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.backup import get_backup_path, maybe_run_daily_backup, run_database_backup
from pupu.memory import get_familiarity_info, init_db, reset_session, save_message, set_familiarity


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        reset_session("owner")
        TEST_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        for path in TEST_BACKUP_DIR.glob("*.db"):
            path.unlink()

    def test_set_familiarity_restores_score_and_level(self):
        set_familiarity(50, "owner")
        info = get_familiarity_info("owner")
        self.assertEqual(info["score"], 50)
        self.assertTrue(info["updated_at"])

    def test_run_database_backup_writes_daily_snapshot(self):
        save_message("user", "hello", "owner", source="chat")
        report = run_database_backup(now=datetime(2026, 4, 26, 3, 5, 0))
        backup_path = get_backup_path(datetime(2026, 4, 26, 3, 5, 0).date())

        self.assertIn(str(backup_path), report)
        self.assertTrue(backup_path.exists())
        self.assertGreater(backup_path.stat().st_size, 0)

    def test_maybe_run_daily_backup_only_runs_once_after_three(self):
        self.assertIsNone(maybe_run_daily_backup(datetime(2026, 4, 26, 2, 59, 0)))
        first = maybe_run_daily_backup(datetime(2026, 4, 26, 3, 1, 0))
        second = maybe_run_daily_backup(datetime(2026, 4, 26, 8, 0, 0))

        self.assertIsNotNone(first)
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
