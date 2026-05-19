import os
from pathlib import Path
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.agent import REVIEW_INTERVAL, chat, _maybe_batch_review
from pupu.facts_report import format_facts_report
from pupu.important_event_report import format_important_events_report
import pupu.proactive as proactive
from pupu.memory import (
    get_familiarity,
    get_summaries,
    init_db,
    reset_session,
    save_message,
    save_summary,
    set_familiarity,
    upsert_self_facts,
    upsert_user_facts,
)
from pupu.memory_index.memu_adapter import (
    MemuWriteResult,
    _build_review_entries,
    _canonical_memory_payload_for_hash,
    _extract_item_id,
    _format_items,
    _memorize_config,
    _retrieve_item_config,
    is_memu_long_term_enabled,
)
from pupu.memory_index import rebuild_memu_session, run_memu_maintenance
from pupu.message_sources import CHAT
from pupu.persona import build_system_prompt


class MemuMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_memu_{self._testMethodName}"
        reset_session(self.session_id)

    def _save_chat_turn(self, index: int):
        save_message("user", f"user-{index}", self.session_id, source=CHAT)
        save_message("assistant", f"assistant-{index}", self.session_id, source=CHAT)

    def test_memu_enabled_requires_embedding_key(self):
        env = {
            "PUPU_MEMU_ENABLED": "true",
            "PUPU_MEMU_API_KEY": "",
            "PUPU_MEMU_EMBED_API_KEY": "",
            "OPENAI_API_KEY": "",
        }
        with patch.dict(os.environ, env):
            self.assertFalse(is_memu_long_term_enabled())

    def test_extract_item_id_accepts_memu_memory_item_response(self):
        result = {
            "memory_item": {
                "id": "memu-item-1",
                "summary": "created",
            },
            "category_updates": [],
        }

        self.assertEqual(_extract_item_id(result), "memu-item-1")

    def test_memu_native_retrieve_and_memorize_configs_are_enabled(self):
        with patch.dict(
            os.environ,
            {
                "PUPU_MEMU_RANKING": "salience",
                "PUPU_MEMU_RECENCY_DECAY_DAYS": "14",
                "PUPU_MEMU_ENABLE_REINFORCEMENT": "true",
                "PUPU_MEMU_NATIVE_CATEGORY_SUMMARIES": "true",
            },
            ):
                retrieve = _retrieve_item_config(6)
                memorize = _memorize_config()

        self.assertIs(retrieve.get("enabled"), True)
        self.assertEqual(retrieve.get("top_k"), 6)
        if "ranking" in retrieve:
            self.assertEqual(retrieve.get("ranking"), "salience")
        if "recency_decay_days" in retrieve:
            self.assertEqual(retrieve.get("recency_decay_days"), 14.0)
        if "enable_item_reinforcement" in memorize:
            self.assertIs(memorize.get("enable_item_reinforcement"), True)
        if "enable_item_references" in memorize:
            self.assertIs(memorize.get("enable_item_references"), True)

    def test_memu_reinforcement_hash_ignores_volatile_pupu_metadata(self):
        first, first_extra = _canonical_memory_payload_for_hash(
            '{"kind":"user_fact","text":"用户喜欢星露谷","source_msg_start_id":1,"created_at":"2026-01-01"}'
        )
        second, second_extra = _canonical_memory_payload_for_hash(
            '{"kind":"user_fact","text":"用户喜欢星露谷","source_msg_start_id":99,"created_at":"2026-05-11"}'
        )

        self.assertEqual(first, second)
        self.assertEqual(first_extra["source_msg_start_id"], 1)
        self.assertEqual(second_extra["source_msg_start_id"], 99)

    def test_memu_reports_merge_payload_extra_saved_after_reinforcement(self):
        items = [
            {
                "id": "m1",
                "summary": '{"kind":"user_fact","text":"用户喜欢星露谷"}',
                "extra": {
                    "pupu_payload_extra": {
                        "created_at": "2026-05-11T12:00:00",
                        "source_msg_start_id": 10,
                    }
                },
            }
        ]

        with patch("pupu.memory_index.memu_adapter._list_items", return_value=items):
            report = _format_items(self.session_id, {"user_fact"}, "empty")

        self.assertIn("用户喜欢星露谷", report)
        self.assertIn("memU 长期记忆 1 条", report)

    def test_adapter_builds_summary_entries(self):
        entries = _build_review_entries(
            summary="用户和仆仆在2026年5月19日晚上讨论星露谷接入。",
            user_facts={"nickname": "xiaofu"},
            self_facts={},
            important_events=[],
        )

        self.assertEqual([kind for kind, _text, _extra in entries], ["summary", "user_fact"])
        self.assertEqual(entries[0][1], "用户和仆仆在2026年5月19日晚上讨论星露谷接入。")
        self.assertEqual(entries[1][1], "nickname: xiaofu")

    def test_adapter_absolutizes_important_event_text_for_memu(self):
        entries = _build_review_entries(
            summary="",
            user_facts={},
            self_facts={},
            important_events=[
                {
                    "source_event_key": "watch-yurucamp",
                    "title": "今晚一起看摇曳露营",
                    "kind": "promise",
                    "event_time": "2026-05-12",
                    "time_text": "今晚",
                    "details": "用户答应今晚和仆仆一起看摇曳露营",
                    "followup_hint": "晚上可以询问用户是否开始看摇曳露营",
                    "confidence": 0.9,
                }
            ],
        )

        self.assertEqual(entries[0][0], "important_event")
        text = entries[0][1]
        self.assertIn("2026年5月12日", text)
        self.assertIn("2026年5月12日晚上一起看摇曳露营", text)
        self.assertIn("2026年5月12日晚上可以询问用户是否开始看摇曳露营", text)
        self.assertNotIn("今晚", text)

    def test_system_prompt_can_include_recalled_memories(self):
        prompt = build_system_prompt(
            50,
            user_facts={},
            summaries=[],
            self_facts={},
            important_events=[],
            recalled_memories=[
                {
                    "kind": "user_fact",
                    "text": "用户最近在玩星露谷，也聊过杀戮尖塔2。",
                }
            ],
        )

        self.assertIn("本轮自然想起的记忆", prompt)
        self.assertIn("[user_fact] 用户最近在玩星露谷", prompt)

    def test_chat_uses_memu_recall_and_two_recent_summaries(self):
        upsert_user_facts({"旧事实": "不应该被直接读取"}, self.session_id)
        upsert_self_facts({"旧设定": "也不应该被直接读取"}, self.session_id)

        save_summary("summary-one-old", 1, 2, self.session_id)
        save_summary("summary-two-recent", 3, 4, self.session_id)
        save_summary("summary-three-latest", 5, 6, self.session_id)

        recalled = [
            {
                "kind": "summary",
                "text": "用户刚刚问起星露谷里的仆仆。",
                "source": "memu",
            }
        ]

        with patch("pupu.agent.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.agent.recall_memories", return_value=recalled) as mock_recall:
                with patch("pupu.agent.get_user_facts", side_effect=AssertionError("old user_facts read")):
                    with patch("pupu.agent.get_self_facts", side_effect=AssertionError("old self_facts read")):
                        with patch(
                                "pupu.agent.get_important_events",
                                side_effect=AssertionError("old important_events read"),
                            ):
                                with patch("pupu.agent.chat_complete", return_value="好呀"):
                                    with patch("pupu.agent._maybe_batch_review", return_value=None):
                                        reply = chat("星露谷里你在干嘛", self.session_id)

        self.assertEqual(reply, "好呀")
        mock_recall.assert_called_once()

    def test_memu_chat_includes_two_latest_summaries_by_time(self):
        save_summary("summary-one-old", 1, 2, self.session_id)
        save_summary("summary-two-recent", 3, 4, self.session_id)
        save_summary("summary-three-latest", 5, 6, self.session_id)

        with patch("pupu.agent.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.agent.recall_memories", return_value=[]):
                with patch("pupu.agent.chat_complete", return_value="ok") as mock_chat:
                    with patch("pupu.agent._maybe_batch_review", return_value=None):
                        reply = chat("hello", self.session_id)

        self.assertEqual(reply, "ok")
        system_prompt = mock_chat.call_args.kwargs["system"]
        self.assertNotIn("summary-one-old", system_prompt)
        self.assertIn("summary-two-recent", system_prompt)
        self.assertIn("summary-three-latest", system_prompt)

    def test_proactive_prompt_uses_memu_recall_when_enabled(self):
        recent = [
            {"role": "user", "content": "我最近在赶项目"},
            {"role": "assistant", "content": "辛苦啦"},
        ]
        recalled = [
            {
                "kind": "user_fact",
                "text": "用户最近在赶项目",
                "source": "memu",
            }
        ]
        period = {"name": "白天", "topics": ["聊点轻松的"]}

        with patch("pupu.proactive.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.proactive.get_recent_messages", return_value=recent):
                with patch("pupu.proactive.recall_memories", return_value=recalled) as mock_recall:
                    with patch("pupu.proactive.get_self_facts", side_effect=AssertionError("old self_facts read")):
                        with patch("pupu.proactive.get_user_facts", side_effect=AssertionError("old user_facts read")):
                            with patch(
                                "pupu.proactive.get_important_events",
                                side_effect=AssertionError("old important_events read"),
                            ):
                                prompt = proactive._build_proactive_prompt(80, period)

        self.assertIn("recalled memories", prompt)
        self.assertIn("[user_fact] 用户最近在赶项目", prompt)
        mock_recall.assert_called_once()

    def test_batch_review_syncs_long_term_memory_to_memu(self):
        for i in range(REVIEW_INTERVAL):
            self._save_chat_turn(i)
        set_familiarity(0, session_id=self.session_id)

        raw = """{
          "summary": "用户和仆仆聊了星露谷接入。",
          "familiarity_delta": 1,
          "user_facts": {"游戏": "用户想在星露谷里和仆仆互动"},
          "self_facts": {"星露谷身份": "仆仆会作为 NPC 出现"},
          "important_events": [{
            "source_event_key": "stardew-pupu",
            "title": "星露谷仆仆计划",
            "kind": "project",
            "event_time": "",
            "time_text": "最近",
            "details": "用户想让仆仆接入星露谷。",
            "followup_hint": "以后聊星露谷时可以自然想起",
            "confidence": 0.9
          }],
          "task_updates": []
        }"""

        with patch("pupu.agent.json_task", return_value=raw):
            with patch("pupu.agent.is_memu_long_term_enabled", return_value=True):
                with patch("pupu.agent.has_successful_memu_sync", return_value=False):
                    with patch(
                        "pupu.agent.sync_review_memory",
                        return_value=MemuWriteResult(status="success", ids=["m1", "m2"]),
                    ) as mock_sync:
                        with patch("pupu.agent.record_memu_sync") as mock_record:
                            _maybe_batch_review(self.session_id)

        mock_sync.assert_called_once()
        sync_kwargs = mock_sync.call_args.kwargs
        self.assertEqual(sync_kwargs["context_session"], self.session_id)
        self.assertEqual(sync_kwargs["identity_session"], self.session_id)
        self.assertEqual(sync_kwargs["summary"], "用户和仆仆聊了星露谷接入。")
        self.assertEqual(sync_kwargs["user_facts"]["游戏"], "用户想在星露谷里和仆仆互动")
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["status"], "success")
        self.assertEqual(mock_record.call_args.kwargs["memu_ids"], ["m1", "m2"])

    def test_batch_review_memu_failure_keeps_core_review_state(self):
        for i in range(REVIEW_INTERVAL):
            self._save_chat_turn(i)
        set_familiarity(0, session_id=self.session_id)

        raw = """{
          "summary": "即使 memU 写入失败，review 游标也要保存。",
          "familiarity_delta": 2,
          "user_facts": {},
          "self_facts": {},
          "important_events": [],
          "task_updates": []
        }"""

        with patch("pupu.agent.json_task", return_value=raw):
            with patch("pupu.agent.is_memu_long_term_enabled", return_value=True):
                with patch("pupu.agent.has_successful_memu_sync", return_value=False):
                    with patch(
                        "pupu.agent.sync_review_memory",
                        return_value=MemuWriteResult(status="failed", ids=[], error="boom"),
                    ):
                        with patch("pupu.agent.record_memu_sync") as mock_record:
                            _maybe_batch_review(self.session_id)

        summaries = get_summaries(self.session_id, limit=1)
        self.assertEqual(summaries[-1]["summary"], "即使 memU 写入失败，review 游标也要保存。")
        self.assertEqual(get_familiarity(self.session_id), 2)
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["status"], "failed")

    def test_reports_prefer_memu_when_enabled_and_fallback_when_disabled(self):
        with patch("pupu.facts_report.format_memu_facts_report", return_value="memu facts"):
            self.assertEqual(format_facts_report(self.session_id), "memu facts")
        with patch(
            "pupu.important_event_report.format_memu_important_events_report",
            return_value="memu events",
        ):
            self.assertEqual(format_important_events_report(self.session_id), "memu events")

        upsert_user_facts({"游戏": "星露谷"}, self.session_id)
        with patch("pupu.facts_report.format_memu_facts_report", return_value=None):
            self.assertIn("游戏: 星露谷", format_facts_report(self.session_id))

    def test_memu_maintenance_deletes_duplicates_and_low_value_items(self):
        deleted_ids = []

        class FakeService:
            async def delete_memory_item(self, *, memory_id, user=None):
                deleted_ids.append(memory_id)
                return {"id": memory_id}

        items = [
            {"id": "a", "summary": '{"kind":"summary","text":"用户喜欢星露谷"}'},
            {"id": "b", "summary": '{"kind":"summary","text":"用户喜欢星露谷"}'},
            {"id": "c", "summary": '{"kind":"user_fact","text":"嗯"}'},
        ]

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._list_items", return_value=items):
                with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                    result = run_memu_maintenance(self.session_id)

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(set(deleted_ids), {"b", "c"})

    def test_rebuild_syncs_context_summaries_and_identity_memory(self):
        context_id = self.session_id + "_context"
        identity_id = self.session_id + "_identity"
        reset_session(context_id)
        reset_session(identity_id)
        save_summary("群聊摘要", 1, 2, context_id)
        upsert_user_facts({"name": "小夫"}, identity_id)

        captured = []

        def fake_sync(**kwargs):
            captured.append(kwargs)
            return MemuWriteResult(status="success", ids=[f"id-{len(captured)}"])

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter.clear_memu_session", return_value=0):
                with patch("pupu.memory_index.memu_adapter.sync_review_memory", side_effect=fake_sync):
                    report = rebuild_memu_session(identity_id, context_id)

        self.assertIn("写入", report)
        self.assertIn("迁移旧摘要 1 条", report)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["context_session"], context_id)
        self.assertEqual(captured[0]["identity_session"], identity_id)
        self.assertEqual(captured[0]["summary"], "群聊摘要")
        self.assertEqual(captured[0]["user_facts"], {})
        self.assertEqual(captured[1]["context_session"], context_id)
        self.assertEqual(captured[1]["identity_session"], identity_id)
        self.assertEqual(captured[1]["summary"], "")
        self.assertEqual(captured[1]["user_facts"], {"name": "小夫"})


if __name__ == "__main__":
    unittest.main()
