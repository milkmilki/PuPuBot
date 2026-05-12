import os
from pathlib import Path
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.agent import _parse_dialogue_output, chat
from pupu.dialogue_loop import cancel_wait_timer, has_wait_timer, schedule_wait_timer
from pupu.memory import init_db, reset_session
from pupu.sessions import OWNER_SESSION


class ChatFollowupWaitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        reset_session(OWNER_SESSION)
        reset_session("private_10001")
        reset_session("group_42")
        cancel_wait_timer(OWNER_SESSION)
        cancel_wait_timer("private_10001")
        cancel_wait_timer("group_42")

    def tearDown(self):
        cancel_wait_timer(OWNER_SESSION)
        cancel_wait_timer("private_10001")
        cancel_wait_timer("group_42")

    def test_parse_dialogue_output_json(self):
        content, should_wait = _parse_dialogue_output(
            '{"content":"你好呀","should_wait":true}'
        )
        self.assertEqual(content, "你好呀")
        self.assertTrue(should_wait)

    def test_parse_dialogue_output_fallback_plain_text(self):
        content, should_wait = _parse_dialogue_output("普通文本回复")
        self.assertEqual(content, "普通文本回复")
        self.assertFalse(should_wait)

    def test_parse_dialogue_output_infers_should_wait_from_question_text(self):
        content, should_wait = _parse_dialogue_output("你今天步数比赛开始吗？")
        self.assertEqual(content, "你今天步数比赛开始吗？")
        self.assertTrue(should_wait)

    def test_parse_dialogue_output_infers_should_wait_when_json_missing_field(self):
        content, should_wait = _parse_dialogue_output(
            '{"content":"步数比赛今天开始还是明天开始，给个准话"}'
        )
        self.assertEqual(content, "步数比赛今天开始还是明天开始，给个准话")
        self.assertTrue(should_wait)

    def test_chat_starts_wait_timer_for_owner_when_should_wait_true(self):
        with patch("pupu.agent.chat_complete", return_value='{"content":"你先忙","should_wait":true}'):
            with patch("pupu.agent._maybe_batch_review", return_value=None):
                reply = chat("hello", session_id=OWNER_SESSION, is_admin=True)

        self.assertEqual(reply, "你先忙")
        self.assertTrue(has_wait_timer(OWNER_SESSION))

    def test_chat_starts_wait_timer_for_private_session(self):
        with patch("pupu.agent.chat_complete", return_value='{"content":"回我","should_wait":true}'):
            with patch("pupu.agent._maybe_batch_review", return_value=None):
                reply = chat("yo", session_id="private_10001", is_admin=False)

        self.assertEqual(reply, "回我")
        self.assertTrue(has_wait_timer("private_10001"))

    def test_chat_does_not_start_wait_timer_for_group_session(self):
        with patch("pupu.agent.chat_complete", return_value='{"content":"在座怎么说？","should_wait":true}'):
            with patch("pupu.agent._maybe_batch_review", return_value=None):
                reply = chat("hi", session_id="group_42", is_admin=False)

        self.assertEqual(reply, "在座怎么说？")
        self.assertFalse(has_wait_timer("group_42"))

    def test_chat_cancels_wait_timer_when_should_wait_false(self):
        schedule_wait_timer(OWNER_SESSION)
        self.assertTrue(has_wait_timer(OWNER_SESSION))
        with patch("pupu.agent.chat_complete", return_value='{"content":"行了","should_wait":false}'):
            with patch("pupu.agent._maybe_batch_review", return_value=None):
                reply = chat("ok", session_id=OWNER_SESSION, is_admin=True)
        self.assertEqual(reply, "行了")
        self.assertFalse(has_wait_timer(OWNER_SESSION))


if __name__ == "__main__":
    unittest.main()
