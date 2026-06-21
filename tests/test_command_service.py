import os
from pathlib import Path
import unittest

from tests.helpers import activate_test_instance


TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.command_service import _format_history
from pupu.memory import init_db, reset_session, save_message
from pupu.message_sources import CHAT, PROACTIVE, SCHEDULED, WAIT_FOLLOWUP


class CommandServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_command_service_{self._testMethodName}"
        reset_session(self.session_id)

    def test_history_labels_internal_sources(self):
        save_message("user", "我本人说的话", self.session_id, source=CHAT)
        save_message("user", "[定时任务「喝水」]\n提醒一下", self.session_id, source=SCHEDULED)
        save_message("user", "[系统触发的追问]\n自然跟进", self.session_id, source=WAIT_FOLLOWUP)
        save_message("assistant", "我主动问一句", self.session_id, source=PROACTIVE)

        text = _format_history(self.session_id, assistant_name="璐璐")

        self.assertIn("你: 我本人说的话", text)
        self.assertIn("系统触发的定时任务: [定时任务「喝水」]", text)
        self.assertIn("系统触发的追问（璐璐）: [系统触发的追问]", text)
        self.assertIn("璐璐主动发出: 我主动问一句", text)
        self.assertNotIn("你: [定时任务", text)
        self.assertNotIn("你: [系统触发的追问", text)


if __name__ == "__main__":
    unittest.main()
