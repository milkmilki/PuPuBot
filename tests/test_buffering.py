import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from plugins.pupu_support import state
from plugins.pupu_support.buffering import buffer_message, debounce_flush
from pupu.sessions import OWNER_SESSION


class BufferingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state.msg_buffers.clear()
        state.debounce_tasks.clear()
        state.session_phase.clear()

    def tearDown(self):
        state.msg_buffers.clear()
        state.debounce_tasks.clear()
        state.session_phase.clear()

    async def test_processing_messages_schedule_next_debounce(self):
        sid = OWNER_SESSION
        state.msg_buffers[sid] = {
            "texts": ["first"],
            "image_urls": [],
            "bot": object(),
            "event": object(),
            "is_admin": True,
            "nickname": "user",
            "session_label": "私聊",
            "reply_prefix": None,
        }

        def fake_chat(*args, **kwargs):
            state.msg_buffers[sid] = {
                "texts": ["second"],
                "image_urls": [],
                "bot": object(),
                "event": object(),
                "is_admin": True,
                "nickname": "user",
                "session_label": "私聊",
                "reply_prefix": None,
            }
            return "reply"

        scheduled = object()
        with patch.object(state, "DEBOUNCE_SECONDS", 0):
            with patch("plugins.pupu_support.buffering.get_familiarity", return_value=50):
                with patch("plugins.pupu_support.buffering.compute_reply_delay", return_value=(0, None)):
                    with patch("plugins.pupu_support.buffering.chat", side_effect=fake_chat):
                        with patch("plugins.pupu_support.buffering.send_segments", new=AsyncMock()):
                            with patch(
                                "plugins.pupu_support.buffering.asyncio.create_task",
                                return_value=scheduled,
                            ) as mock_create:
                                await debounce_flush(sid)

        self.assertIs(state.debounce_tasks[sid], scheduled)
        self.assertEqual(state.msg_buffers[sid]["texts"], ["second"])
        created_coro = mock_create.call_args.args[0]
        created_coro.close()

    async def test_no_new_messages_does_not_schedule_next_debounce(self):
        sid = OWNER_SESSION
        state.msg_buffers[sid] = {
            "texts": ["first"],
            "image_urls": [],
            "bot": object(),
            "event": object(),
            "is_admin": True,
            "nickname": "user",
            "session_label": "私聊",
            "reply_prefix": None,
        }

        with patch.object(state, "DEBOUNCE_SECONDS", 0):
            with patch("plugins.pupu_support.buffering.get_familiarity", return_value=50):
                with patch("plugins.pupu_support.buffering.compute_reply_delay", return_value=(0, None)):
                    with patch("plugins.pupu_support.buffering.chat", return_value="reply"):
                        with patch("plugins.pupu_support.buffering.send_segments", new=AsyncMock()):
                            with patch(
                                "plugins.pupu_support.buffering.asyncio.create_task",
                                return_value=Mock(),
                            ) as mock_create:
                                await debounce_flush(sid)

        mock_create.assert_not_called()
        self.assertNotIn(sid, state.msg_buffers)
        self.assertNotIn(sid, state.debounce_tasks)

    async def test_owner_message_cancels_wait_followup_timer(self):
        sid = state.OWNER_SESSION
        scheduled = object()
        with patch(
            "plugins.pupu_support.buffering.cancel_wait_timer",
            return_value=True,
        ) as mock_cancel:
            with patch(
                "plugins.pupu_support.buffering.asyncio.create_task",
                return_value=scheduled,
            ) as mock_create:
                await buffer_message(
                    sid=sid,
                    text="hi",
                    image_urls=[],
                    bot=object(),
                    event=object(),
                    is_admin=True,
                    nickname="owner",
                    session_label="私聊",
                )

        mock_cancel.assert_called_once_with(sid)
        self.assertIn(sid, state.debounce_tasks)
        self.assertIs(state.debounce_tasks[sid], scheduled)
        created_coro = mock_create.call_args.args[0]
        created_coro.close()


if __name__ == "__main__":
    unittest.main()
