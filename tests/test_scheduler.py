import os
from pathlib import Path
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.memory import (
    create_scheduled_task,
    get_due_scheduled_tasks,
    init_db,
    list_scheduled_tasks,
    reset_session,
)
from pupu.scheduler import _is_wait_followup_task, _latest_message_is_user, _onebot_send
from pupu.sessions import OWNER_SESSION


async def _no_sleep(_seconds):
    return None


class FakeBot:
    def __init__(self):
        self.private_messages = []
        self.group_messages = []

    async def send_private_msg(self, *, user_id: int, message: str):
        self.private_messages.append((user_id, message))

    async def send_group_msg(self, *, group_id: int, message: str):
        self.group_messages.append((group_id, message))


class SchedulerSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_owner_send_splits_lines_and_logs(self):
        bot = FakeBot()
        with patch("pupu.scheduler._load_first_numeric_owner_qq", return_value=123):
            with patch("pupu.scheduler.asyncio.sleep", _no_sleep):
                with patch("builtins.print") as mock_print:
                    await _onebot_send(bot, OWNER_SESSION, "第一句\n第二句\n\n第三句")

        self.assertEqual(
            bot.private_messages,
            [(123, "第一句"), (123, "第二句"), (123, "第三句")],
        )
        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn(">>> 发送 | 私聊 | 123 | 第一句", printed)

    async def test_scheduled_group_send_splits_lines_and_logs(self):
        bot = FakeBot()
        with patch("pupu.scheduler.asyncio.sleep", _no_sleep):
            with patch("builtins.print") as mock_print:
                await _onebot_send(bot, "group_456", "第一句\n第二句")

        self.assertEqual(bot.group_messages, [(456, "第一句"), (456, "第二句")])
        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn(">>> 发送 | 群456 | 456 | 第一句", printed)


class SchedulerGuardTests(unittest.TestCase):
    def test_is_wait_followup_task_detects_prefix(self):
        self.assertTrue(_is_wait_followup_task({"title": "wait_followup:owner"}))
        self.assertTrue(_is_wait_followup_task({"title": "WAIT_FOLLOWUP:any"}))
        self.assertFalse(_is_wait_followup_task({"title": "提醒"}))

    def test_latest_message_is_user(self):
        with patch("pupu.scheduler.get_recent_messages", return_value=[{"role": "user"}]):
            self.assertTrue(_latest_message_is_user(OWNER_SESSION))
        with patch("pupu.scheduler.get_recent_messages", return_value=[{"role": "assistant"}]):
            self.assertFalse(_latest_message_is_user(OWNER_SESSION))
        with patch("pupu.scheduler.get_recent_messages", return_value=[]):
            self.assertFalse(_latest_message_is_user(OWNER_SESSION))


class SchedulerDueWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_scheduler_due_{self._testMethodName}"
        reset_session(self.session_id)

    def test_due_task_within_one_hour_is_returned(self):
        task_id = create_scheduled_task(
            self.session_id,
            "near due",
            "near due instruction",
            "2026-05-01T09:00:00",
            "once",
            None,
        )

        tasks = get_due_scheduled_tasks("2026-05-01T10:00:00", 20)

        self.assertIn(task_id, [int(task["id"]) for task in tasks])

    def test_due_task_older_than_one_hour_is_skipped_and_removed(self):
        task_id = create_scheduled_task(
            self.session_id,
            "stale due",
            "stale due instruction",
            "2026-05-01T08:59:59",
            "once",
            None,
        )

        tasks = get_due_scheduled_tasks("2026-05-01T10:00:00", 20)

        self.assertNotIn(task_id, [int(task["id"]) for task in tasks])
        self.assertEqual(list_scheduled_tasks(self.session_id), [])

    def test_missed_recurring_task_advances_without_triggering(self):
        task_id = create_scheduled_task(
            self.session_id,
            "daily stale due",
            "daily stale due instruction",
            "2026-05-01T08:00:00",
            "daily",
            None,
        )

        tasks = get_due_scheduled_tasks("2026-05-01T10:00:00", 20)
        remaining = list_scheduled_tasks(self.session_id)

        self.assertNotIn(task_id, [int(task["id"]) for task in tasks])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["run_at"], "2026-05-02T08:00:00")


if __name__ == "__main__":
    unittest.main()
