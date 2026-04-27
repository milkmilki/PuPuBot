import os
from pathlib import Path
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.scheduler import _onebot_send


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
                    await _onebot_send(bot, "owner", "第一句\n第二句\n\n第三句")

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


if __name__ == "__main__":
    unittest.main()
