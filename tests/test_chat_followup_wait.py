import os
from pathlib import Path
import unittest
from tests.helpers import activate_test_instance
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"

from pupu.agent import _parse_dialogue_output, chat
from pupu.dialogue_loop import cancel_wait_timer, has_wait_timer, schedule_wait_timer
from pupu.memory import init_db, reset_session, save_message_with_speaker, set_familiarity
from pupu.sessions import OWNER_SESSION
from pupu.storage import get_conn, upsert_person


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

    def test_group_chat_uses_people_context_and_natural_history(self):
        set_familiarity(100, session_id=OWNER_SESSION)
        set_familiarity(50, session_id="private_3853876778")
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "owner",
                kind="owner",
                display_name="小夫",
                qq_id="424225912",
            )
            upsert_person(
                conn,
                "qq:3853876778",
                kind="qq",
                display_name="仆仆",
                qq_id="3853876778",
            )
            conn.commit()
        finally:
            conn.close()

        payload = (
            '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"},'
            '{"person_key":"qq:3853876778","display_name":"仆仆","qq_id":"3853876778","kind":"qq"}]'
        )
        save_message_with_speaker(
            "user",
            "[时间: 2026-06-19 周五 08:10] [钮钴禄·大家大宁(QQ:424225912)] 姐姐们\n"
            "[仆仆(QQ:3853876778)] 仆仆：又来了",
            "group_42",
            speaker_key=payload,
            speaker_name="钮钴禄·大家大宁",
            speaker_qq="424225912",
        )
        save_message_with_speaker(
            "assistant",
            "我在",
            "group_42",
            speaker_key="instance",
            speaker_name="璐璐",
            speaker_qq="",
        )

        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch("pupu.persona.builder.get_pupu_name", return_value="璐璐"):
                with patch("pupu.agent.chat_complete", return_value='{"content":"别闹","should_wait":false}') as mock_chat:
                    with patch("pupu.agent._maybe_batch_review", return_value=None):
                        reply = chat(
                            "继续",
                            session_id="group_42",
                            identity_session=OWNER_SESSION,
                            is_admin=False,
                            persist_user=False,
                            speaker_key=payload,
                            speaker_name="钮钴禄·大家大宁",
                            speaker_qq="424225912",
                        )

        self.assertEqual(reply, "别闹")
        kwargs = mock_chat.call_args.kwargs
        self.assertNotIn("## 当前关系：恋人", kwargs["system"])
        self.assertIn("## 当前群聊人物", kwargs["system"])
        self.assertIn("你是璐璐。", kwargs["system"])
        self.assertIn("小夫：与你的关系是恋人。", kwargs["system"])
        self.assertIn("仆仆：与你的关系是朋友。", kwargs["system"])
        joined_messages = "\n".join(item["content"] for item in kwargs["messages"])
        self.assertIn("小夫：姐姐们", joined_messages)
        self.assertIn("仆仆：又来了", joined_messages)
        self.assertIn("我在", joined_messages)
        self.assertNotIn("“恋人”", joined_messages)
        self.assertNotIn("“朋友”", joined_messages)
        self.assertNotIn("“自己”", joined_messages)
        self.assertNotIn("仆仆：仆仆：", joined_messages)
        assistant_messages = [item for item in kwargs["messages"] if item["role"] == "assistant"]
        self.assertTrue(any(item["content"] == "我在" for item in assistant_messages))

    def test_chat_cancels_wait_timer_when_should_wait_false(self):
        schedule_wait_timer(OWNER_SESSION)
        self.assertTrue(has_wait_timer(OWNER_SESSION))
        with patch("pupu.agent.chat_complete", return_value='{"content":"行了","should_wait":false}'):
            with patch("pupu.agent._maybe_batch_review", return_value=None):
                reply = chat("ok", session_id=OWNER_SESSION, is_admin=True)
        self.assertEqual(reply, "行了")
        self.assertFalse(has_wait_timer(OWNER_SESSION))

    def test_chat_strips_instance_name_prefix_from_model_reply(self):
        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch("pupu.agent.chat_complete", return_value='{"content":"璐璐：真睡了？","should_wait":true}'):
                with patch("pupu.agent._maybe_batch_review", return_value=None):
                    reply = chat("hi", session_id=OWNER_SESSION, is_admin=True)

        self.assertEqual(reply, "真睡了？")

    def test_chat_strips_relationship_instance_prefix_from_model_reply(self):
        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch(
                "pupu.agent.chat_complete",
                return_value='{"content":"“自己”璐璐：嗯……\\n\\n你倒是精神了","should_wait":false}',
            ):
                with patch("pupu.agent._maybe_batch_review", return_value=None):
                    reply = chat("hi", session_id="group_42", is_admin=True)

        self.assertEqual(reply, "嗯……\n\n你倒是精神了")

    def test_group_chat_strips_other_speaker_prefix_from_model_reply(self):
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "owner",
                kind="owner",
                display_name="小夫",
                qq_id="424225912",
            )
            conn.commit()
        finally:
            conn.close()

        payload = '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"}]'
        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch(
                "pupu.agent.chat_complete",
                return_value='{"content":"“恋人”小夫：呜……（慢慢挪过来）","should_wait":false}',
            ):
                with patch("pupu.agent._maybe_batch_review", return_value=None):
                    reply = chat(
                        "继续",
                        session_id="group_42",
                        identity_session=OWNER_SESSION,
                        is_admin=False,
                        persist_user=False,
                        speaker_key=payload,
                        speaker_name="钮钴禄·大家大宁",
                        speaker_qq="424225912",
                    )

        self.assertEqual(reply, "呜……（慢慢挪过来）")


if __name__ == "__main__":
    unittest.main()
