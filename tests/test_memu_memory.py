import os
import json
from pathlib import Path
import unittest
from tests.helpers import activate_test_instance
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.agent import REVIEW_INTERVAL, chat, _maybe_batch_review
from pupu.facts_report import format_facts_report
from pupu.event_thread_report import format_event_threads_report
import pupu.proactive as proactive
from pupu.memory import (
    get_familiarity,
    get_summaries,
    init_db,
    reset_session,
    save_message,
    save_summary,
    set_familiarity,
    upsert_event_threads,
    upsert_person_facts,
)
from pupu.storage.people import person_from_session
from pupu.memory_index.memu_adapter import (
    MemuWriteResult,
    _build_review_entries,
    _canonical_memory_payload_for_hash,
    _extract_item_id,
    _format_history_for_recall,
    _format_items,
    _list_items,
    _memorize_config,
    _retrieve_item_config,
    clear_memu_session,
    is_memu_long_term_enabled,
    recall_memories,
    sync_missing_memu_event_threads,
)
from pupu.memory_index import run_memu_maintenance, run_memu_tidy
from pupu.message_sources import CHAT, PROACTIVE, SCHEDULED, WAIT_FOLLOWUP
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
            '{"kind":"person_fact","text":"小夫 | 喜好: 喜欢像素农场","source_msg_start_id":1,"created_at":"2026-01-01"}'
        )
        second, second_extra = _canonical_memory_payload_for_hash(
            '{"kind":"person_fact","text":"小夫 | 喜好: 喜欢像素农场","source_msg_start_id":99,"created_at":"2026-05-11"}'
        )

        self.assertEqual(first, second)
        self.assertEqual(first_extra["source_msg_start_id"], 1)
        self.assertEqual(second_extra["source_msg_start_id"], 99)

    def test_memu_reinforcement_keeps_source_card_metadata_in_stable_payload(self):
        stable, volatile = _canonical_memory_payload_for_hash(
            json.dumps(
                {
                    "kind": "event_thread",
                    "text": "Event text",
                    "projection_kind": "rag_card",
                    "source_type": "event_thread",
                    "source_id": 7,
                    "source_key": "event_thread:owner:test",
                    "source_version": "v1",
                    "source_msg_start_id": 1,
                    "created_at": "2026-01-01T00:00:00",
                }
            )
        )

        payload = json.loads(stable)
        self.assertEqual(payload["projection_kind"], "rag_card")
        self.assertEqual(payload["source_type"], "event_thread")
        self.assertEqual(payload["source_key"], "event_thread:owner:test")
        self.assertEqual(payload["source_version"], "v1")
        self.assertNotIn("source_msg_start_id", payload)
        self.assertEqual(volatile["source_msg_start_id"], 1)

    def test_memu_reports_merge_payload_extra_saved_after_reinforcement(self):
        items = [
            {
                "id": "m1",
                "summary": '{"kind":"person_fact","text":"小夫 | 喜好: 喜欢像素农场"}',
                "extra": {
                    "pupu_payload_extra": {
                        "created_at": "2026-05-11T12:00:00",
                        "source_msg_start_id": 10,
                    }
                },
            }
        ]

        with patch("pupu.memory_index.memu_adapter._list_items", return_value=items):
            report = _format_items(self.session_id, {"person_fact"}, "empty")

        self.assertIn("小夫 | 喜好: 喜欢像素农场", report)
        self.assertIn("memU 长期记忆 1 条", report)

    def test_memu_report_preserves_persisted_person_names(self):
        items = [
            {
                "id": "m1",
                "summary": '{"kind":"person_fact","text":"仆仆 | 喜好: 被用户叫姐姐"}',
            }
        ]

        with patch("pupu.memory_index.memu_adapter.get_pupu_name", return_value="璐璐"):
            with patch("pupu.memory_index.memu_adapter._list_items", return_value=items):
                report = _format_items(self.session_id, {"person_fact"}, "empty")

        self.assertIn("仆仆 | 喜好: 被用户叫姐姐", report)

    def test_recall_uses_global_memu_cache_scope(self):
        calls = []

        class FakeService:
            async def retrieve(self, *, queries, where=None):
                calls.append({"queries": queries, "where": where})
                return {
                    "items": [
                        {
                            "id": "group-summary",
                            "summary": '{"kind":"summary","text":"小夫在群聊中与仆仆、璐璐约好晚上看番。"}',
                            "score": 0.9,
                        }
                    ]
                }

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                memories = recall_memories(
                    query="晚上看番",
                    context_session="owner",
                    identity_session="owner",
                    history=[],
                )

        self.assertEqual(calls[0]["where"], {})
        self.assertEqual(memories[0]["text"], "小夫在群聊中与仆仆、璐璐约好晚上看番。")

    def test_list_items_uses_global_memu_cache_scope(self):
        calls = []

        class FakeService:
            async def list_memory_items(self, *, where=None):
                calls.append(where)
                return {
                    "items": [
                        {"id": "private-item", "summary": '{"kind":"person_fact","text":"小夫 | 记忆: 私聊记忆"}'},
                        {"id": "group-item", "summary": '{"kind":"summary","text":"群聊记忆"}'},
                    ]
                }

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                items = _list_items("owner")

        self.assertEqual(calls, [{}])
        self.assertEqual([item["id"] for item in items], ["private-item", "group-item"])

    def test_clear_memu_session_clears_global_memu_cache_scope(self):
        calls = []

        class FakeService:
            async def clear_memory(self, *, where=None):
                calls.append(where)
                return {
                    "deleted_items": [{"id": "private-item"}, {"id": "group-item"}],
                    "deleted_categories": [],
                    "deleted_resources": [],
                }

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                deleted = clear_memu_session("owner")

        self.assertEqual(calls, [{}])
        self.assertEqual(deleted, 2)

    def test_adapter_builds_summary_entries(self):
        with patch("pupu.memory_index.memu_adapter.get_pupu_name", return_value="璐璐"):
            entries = _build_review_entries(
                summary="用户和仆仆在2026年5月19日晚上讨论像素农场联动。",
                person_facts=[
                    {
                        "subject_display_name": "小夫",
                        "subject_person_key": "owner",
                        "scope": "person",
                        "fact_key": "nickname",
                        "fact_value": "xiaofu",
                    }
                ],
                event_threads=[],
            )

        self.assertEqual([kind for kind, _text, _extra in entries], ["summary", "person_fact"])
        self.assertEqual(
            entries[0][1],
            "对话摘要（用户 / 璐璐）: 用户和仆仆在2026年5月19日晚上讨论像素农场联动。",
        )
        self.assertEqual(entries[1][1], "小夫 | nickname: xiaofu")

    def test_recall_history_uses_user_and_character_name(self):
        text = _format_history_for_recall(
            [
                {"role": "user", "content": "我想画画"},
                {"role": "assistant", "content": "我想买二手屏"},
            ],
            character_name="璐璐",
        )

        self.assertIn("用户: 我想画画", text)
        self.assertIn("璐璐: 我想买二手屏", text)

    def test_recall_history_labels_internal_sources(self):
        text = _format_history_for_recall(
            [
                {"role": "user", "content": "[定时任务「喝水」]\n提醒一下", "source": SCHEDULED},
                {"role": "user", "content": "[系统触发的追问]\n自然跟进", "source": WAIT_FOLLOWUP},
                {"role": "assistant", "content": "我主动问一句", "source": PROACTIVE},
            ],
            character_name="璐璐",
        )

        self.assertIn("系统触发的定时任务: [定时任务「喝水」]", text)
        self.assertIn("系统触发的追问（璐璐）: [系统触发的追问]", text)
        self.assertIn("璐璐主动发出: 我主动问一句", text)
        self.assertNotIn("用户: [定时任务", text)

    def test_recall_history_keeps_preformatted_group_speaker_names(self):
        text = _format_history_for_recall(
            [
                {"role": "user", "content": "[时间: 2026-06-19 周五 08:01] 小夫：姐姐们"},
                {"role": "user", "content": "仆仆：又来了"},
            ],
            character_name="璐璐",
        )

        self.assertIn("[时间: 2026-06-19 周五 08:01] 小夫：姐姐们", text)
        self.assertIn("仆仆：又来了", text)
        self.assertNotIn("用户: [时间:", text)
        self.assertNotIn("用户: 仆仆：", text)

    def test_adapter_absolutizes_event_thread_text_for_memu(self):
        with patch("pupu.memory_index.memu_adapter.get_pupu_name", return_value="璐璐"):
            entries = _build_review_entries(
                summary="",
                event_threads=[
                    {
                        "thread_key": "watch-yurucamp",
                        "title": "今晚一起看摇曳露营",
                        "kind": "promise",
                        "event_time": "2026-05-12",
                        "time_text": "今晚",
                        "details": "用户答应今晚和仆仆一起看摇曳露营",
                        "followup_hint": "晚上可以询问用户是否开始看摇曳露营",
                        "confidence": 0.9,
                        "people_label": "小夫 / 璐璐",
                    }
                ],
            )

        self.assertEqual(entries[0][0], "event_thread")
        text = entries[0][1]
        self.assertIn("2026年5月12日", text)
        self.assertIn("2026年5月12日晚上一起看摇曳露营", text)
        self.assertIn("用户答应2026年5月12日晚上和仆仆一起看摇曳露营", text)
        self.assertIn("2026年5月12日晚上可以询问用户是否开始看摇曳露营", text)
        self.assertIn("相关人物: 小夫 / 璐璐", text)
        self.assertNotIn("今晚", text)

    def test_adapter_preserves_other_instance_name_in_group_event(self):
        with patch("pupu.memory_index.memu_adapter.get_pupu_name", return_value="璐璐"):
            entries = _build_review_entries(
                summary="小夫在群聊中提出想晚上与仆仆、璐璐亲密。",
                event_threads=[
                    {
                        "thread_key": "xiaofu-pupu-lulu",
                        "title": "小夫与仆仆、璐璐的约定",
                        "kind": "promise",
                        "event_time": "2026-06-18",
                        "details": "仆仆和璐璐要求小夫先干完活。",
                        "followup_hint": "晚上看小夫表现。",
                        "confidence": 0.9,
                        "people_label": "仆仆 / 小夫 / 璐璐",
                    }
                ],
            )

        summary_text = entries[0][1]
        event_text = entries[1][1]
        self.assertIn("仆仆、璐璐", summary_text)
        self.assertIn("相关人物: 仆仆 / 小夫 / 璐璐", event_text)
        self.assertIn("小夫与仆仆、璐璐的约定", event_text)
        self.assertIn("仆仆和璐璐要求小夫先干完活", event_text)

    def test_system_prompt_can_include_recalled_memories(self):
        with patch("pupu.persona.builder.get_pupu_name", return_value="璐璐"):
            prompt = build_system_prompt(
                50,
                summaries=[],
                event_threads=[],
                recalled_memories=[
                    {
                        "kind": "person_fact",
                        "text": "小夫 | 近况: 最近在玩像素农场，也聊过杀戮尖塔2。",
                    }
                ],
            )

        self.assertIn("本轮自然想起的事", prompt)
        self.assertIn("- 小夫 | 近况: 最近在玩像素农场", prompt)
        self.assertNotIn("[person_fact | 相关人物]", prompt)

    def test_system_prompt_preserves_names_inside_recalled_memory(self):
        with patch("pupu.persona.builder.get_pupu_name", return_value="璐璐"):
            prompt = build_system_prompt(
                50,
                summaries=[{"summary": "小夫在群聊中想和仆仆、璐璐一起看番。"}],
                event_threads=[
                    {
                        "title": "小夫与仆仆、璐璐的约定",
                        "details": "仆仆和璐璐要求小夫先干完活。",
                    }
                ],
                recalled_memories=[
                    {
                        "kind": "event_thread",
                        "text": "相关人物: 仆仆 / 小夫 / 璐璐; 小夫与仆仆、璐璐的约定",
                    }
                ],
            )

        self.assertIn("小夫在群聊中想和仆仆、璐璐一起看番", prompt)
        self.assertIn("小夫与仆仆、璐璐的约定", prompt)
        self.assertIn("仆仆和璐璐要求小夫先干完活", prompt)
        self.assertNotIn("璐璐、璐璐", prompt)

    def test_system_prompt_anchors_current_instance_name(self):
        with patch("pupu.persona.builder.get_pupu_name", return_value="璐璐"):
            prompt = build_system_prompt(50)

        self.assertIn("你就是璐璐", prompt)
        self.assertIn("用户现在是在和璐璐说话", prompt)
        self.assertIn("你叫璐璐", prompt)
        self.assertNotIn("你叫仆仆", prompt)

    def test_chat_uses_memu_recall_and_two_recent_summaries(self):
        upsert_person_facts(
            {"旧事实": "不应该被直接读取"},
            default_subject_person_key=person_from_session(self.session_id),
        )

        save_summary("summary-one-old", 1, 2, self.session_id)
        save_summary("summary-two-recent", 3, 4, self.session_id)
        save_summary("summary-three-latest", 5, 6, self.session_id)

        recalled = [
            {
                "kind": "summary",
                "text": "用户刚刚问起像素农场里的仆仆。",
                "source": "memu",
            }
        ]

        with patch("pupu.agent.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.agent.recall_memories", return_value=recalled) as mock_recall:
                with patch(
                    "pupu.agent.get_event_threads",
                    side_effect=AssertionError("event_threads should not be read directly when memU recall is enabled"),
                ):
                    with patch("pupu.agent.chat_complete", return_value="好呀"):
                        with patch("pupu.agent._maybe_batch_review", return_value=None):
                            reply = chat("像素农场里你在干嘛", self.session_id)

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
                "kind": "person_fact",
                "text": "小夫 | 近况: 最近在赶项目",
                "source": "memu",
            }
        ]
        period = {"name": "白天", "topics": ["聊点轻松的"]}

        with patch("pupu.proactive.get_pupu_name", return_value="璐璐"):
            with patch("pupu.proactive.is_memu_long_term_enabled", return_value=True):
                with patch("pupu.proactive.get_recent_messages", return_value=recent):
                    with patch("pupu.proactive.recall_memories", return_value=recalled) as mock_recall:
                        with patch(
                            "pupu.proactive.get_event_threads",
                            side_effect=AssertionError("event_threads should not be read directly when memU recall is enabled"),
                        ):
                            prompt = proactive._build_proactive_prompt(80, period)

        self.assertIn("自然想起的长期记忆（用户 / 璐璐）", prompt)
        self.assertIn("[person_fact | 相关人物] 小夫 | 近况: 最近在赶项目", prompt)
        mock_recall.assert_called_once()

    def test_proactive_context_uses_thirty_recent_messages_and_two_summaries(self):
        recent = [{"role": "user", "content": "最近消息"}]
        summaries = [{"summary": "最近摘要"}]

        with patch("pupu.proactive.get_recent_messages", return_value=recent) as mock_recent:
            with patch("pupu.proactive.get_summaries", return_value=summaries) as mock_summaries:
                loaded_recent, loaded_summaries = proactive._load_proactive_context()

        self.assertEqual(loaded_recent, recent)
        self.assertEqual(loaded_summaries, summaries)
        mock_recent.assert_called_once_with(
            proactive.PROACTIVE_HISTORY_LIMIT,
            proactive.OWNER_SESSION,
        )
        mock_summaries.assert_called_once_with(
            proactive.OWNER_SESSION,
            limit=proactive.PROACTIVE_SUMMARY_LIMIT,
        )

    def test_proactive_prompt_includes_recent_summaries_and_full_recent_context(self):
        recent = [
            {"role": "user", "content": "第一条最近消息"},
            {"role": "assistant", "content": "第二条最近回复"},
        ]
        summaries = [
            {"summary": "summary-two-recent"},
            {"summary": "summary-three-latest"},
        ]
        period = {"name": "白天", "topics": ["聊点轻松的"]}

        with patch("pupu.proactive.get_pupu_name", return_value="璐璐"):
            with patch("pupu.proactive.is_memu_long_term_enabled", return_value=False):
                with patch("pupu.proactive._load_proactive_context", return_value=(recent, summaries)):
                    with patch("pupu.proactive.get_person_facts", return_value=[]):
                        with patch("pupu.proactive.get_event_threads", return_value=[]):
                            prompt = proactive._build_proactive_prompt(80, period)

        self.assertIn("## 之前聊过的摘要", prompt)
        self.assertIn("## 最近上下文记录", prompt)
        self.assertIn("summary-two-recent", prompt)
        self.assertIn("summary-three-latest", prompt)
        self.assertIn("用户: 第一条最近消息", prompt)
        self.assertIn("璐璐: 第二条最近回复", prompt)

    def test_proactive_context_labels_system_triggered_messages(self):
        recent = [
            {"role": "user", "content": "[定时任务「喝水」]\n提醒一下", "source": SCHEDULED},
            {"role": "user", "content": "你刚才要不要继续问一句", "source": WAIT_FOLLOWUP},
            {"role": "assistant", "content": "我主动问一句", "source": PROACTIVE},
            {"role": "user", "content": "我本人说的话", "source": CHAT},
        ]
        with patch("pupu.proactive.get_pupu_name", return_value="璐璐"):
            text = proactive._format_recent_context(recent)

        self.assertIn("系统触发的定时任务: [定时任务「喝水」]", text)
        self.assertIn("系统触发的追问（璐璐）: 你刚才要不要继续问一句", text)
        self.assertIn("璐璐主动发出: 我主动问一句", text)
        self.assertIn("用户: 我本人说的话", text)
        self.assertNotIn("用户: [定时任务", text)

    def test_batch_review_syncs_long_term_memory_to_memu(self):
        for i in range(REVIEW_INTERVAL):
            self._save_chat_turn(i)
        set_familiarity(0, session_id=self.session_id)

        raw = """{
          "summary": "用户和仆仆聊了像素农场联动。",
          "familiarity_delta": 1,
          "fact_updates": [
            {"action": "create", "subject": "小夫", "scope": "person", "key": "游戏", "value": "想在像素农场里和仆仆互动"},
            {"action": "create", "subject": "仆仆", "scope": "person", "key": "像素农场身份", "value": "会作为 NPC 出现"}
          ],
          "event_updates": [{
            "action": "create_thread",
            "thread_key": "farm-game-pupu",
            "title": "像素农场仆仆计划",
            "kind": "project",
            "event_time": "",
            "time_text": "最近",
            "summary": "用户想让仆仆接入像素农场。",
            "followup_hint": "以后聊像素农场时可以自然想起",
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
        self.assertEqual(sync_kwargs["summary"], "用户和仆仆聊了像素农场联动。")
        fact_values = {
            row["fact_key"]: row["fact_value"]
            for row in sync_kwargs["person_facts"]
        }
        self.assertEqual(fact_values["游戏"], "想在像素农场里和仆仆互动")
        self.assertEqual(sync_kwargs["event_threads"][0]["thread_key"], "farm-game-pupu")
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
          "fact_updates": [],
          "event_updates": [],
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

    def test_reports_use_local_store_for_facts_and_events(self):
        self.assertNotEqual(format_event_threads_report(self.session_id, sync_memu=False), "memu events")

        upsert_person_facts(
            {"游戏": "像素农场"},
            default_subject_person_key=person_from_session(self.session_id),
        )
        self.assertIn("游戏: 像素农场", format_facts_report(self.session_id))

    def test_sync_missing_memu_event_threads_backfills_by_source_key(self):
        created = []

        class FakeService:
            async def list_memory_items(self, *, where=None):
                return {"items": []}

            async def create_memory_item(self, **kwargs):
                created.append(kwargs)
                return {"memory_item": {"id": f"new-{len(created)}"}}

        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "missing-event",
                    "title": "Missing event",
                    "kind": "milestone",
                    "details": "needs sync",
                    "confidence": 0.9,
                    "status": "active",
                },
            ],
        )

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                result = sync_missing_memu_event_threads(self.session_id, [])

        self.assertEqual(result["status"], "synced")
        self.assertGreaterEqual(result["checked"], 1)
        self.assertGreaterEqual(result["missing"], 1)
        self.assertGreaterEqual(result["created"], 1)
        payloads = [json.loads(item["memory_content"]) for item in created]
        self.assertTrue(all(payload["projection_kind"] == "rag_card" for payload in payloads))
        self.assertTrue(
            any(
                payload["source_type"] == "event_thread"
                and "missing-event" in payload["source_key"]
                for payload in payloads
            )
        )

    def legacy_memu_maintenance_deletes_duplicates_and_low_value_items(self):
        deleted_ids = []

        class FakeService:
            async def delete_memory_item(self, *, memory_id, user=None):
                deleted_ids.append(memory_id)
                return {"id": memory_id}

        items = [
            {"id": "a", "summary": '{"kind":"summary","text":"用户喜欢像素农场"}'},
            {"id": "b", "summary": '{"kind":"summary","text":"用户喜欢像素农场"}'},
            {"id": "c", "summary": '{"kind":"person_fact","text":"小夫 | 临时状态: 嗯"}'},
        ]

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._list_items", return_value=items):
                with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                    result = run_memu_maintenance(self.session_id)

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(set(deleted_ids), {"c"})

    def legacy_memu_tidy_apply_deletes_judge_selected_items_and_skips_summary(self):
        deleted_ids = []

        class FakeService:
            async def delete_memory_item(self, *, memory_id, user=None):
                deleted_ids.append(memory_id)
                return {"id": memory_id}

        items = [
            {"id": "summary-1", "summary": '{"kind":"summary","text":"这条摘要不该进入 tidy"}'},
            {
                "id": "dup-fact",
                "summary": '{"kind":"person_fact","key":"nickname","text":"小夫 | nickname: 小夫","created_at":"2026-05-11T12:00:00"}',
                "memory_type": "profile",
            },
            {
                "id": "junk-fact",
                "summary": '{"kind":"person_fact","key":"temp_note","text":"小夫 | temp_note: True","created_at":"2026-05-11T12:00:00"}',
                "memory_type": "profile",
            },
        ]

        judge_response = json.dumps(
            {
                "drop_ids": ["dup-fact", "junk-fact"],
                "reason_by_id": {
                    "dup-fact": "重复",
                    "junk-fact": "低价值",
                },
                "notes": "删掉重复和无用项",
            },
            ensure_ascii=False,
        )

        with patch("pupu.memory_index.memu_tidy.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_tidy._list_items", return_value=items):
                with patch("pupu.memory_index.memu_tidy._get_service", return_value=FakeService()):
                    with patch(
                        "pupu.memory_index.memu_tidy._sync_event_threads_after_tidy",
                        return_value={"status": "ok", "checked": 0, "missing": 0, "synced": 0},
                    ):
                        with patch("pupu.memory_index.memu_tidy.json_task", return_value=judge_response) as mock_json:
                            result = run_memu_tidy(self.session_id, mode="apply")

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["source_deleted"], 0)
        self.assertEqual(result["local_deleted"], 0)
        self.assertEqual(set(deleted_ids), {"dup-fact", "junk-fact"})
        self.assertEqual(result["reason_counts"], {"重复": 1, "低价值": 1})
        payload = json.loads(mock_json.call_args.kwargs["user_content"])
        self.assertNotIn("summary-1", json.dumps(payload, ensure_ascii=False))
        self.assertTrue(all(item["kind"] != "summary" for item in payload["items"]))
        for item in payload["items"]:
            self.assertNotIn("source_key", item)
            self.assertNotIn("source_table", item)
            self.assertNotIn("source_action", item)
            self.assertNotIn("delete_source", item)

    def legacy_memu_tidy_does_not_delete_local_event_thread_source(self):
        deleted_ids = []

        class FakeService:
            async def delete_memory_item(self, *, memory_id, user=None):
                deleted_ids.append(memory_id)
                return {"id": memory_id}

        items = [
            {
                "id": "stale-event-index",
                "summary": (
                    '{"kind":"event_thread","thread_key":"old-event",'
                    '"text":"旧事件索引副本","created_at":"2026-05-11T12:00:00"}'
                ),
                "memory_type": "event",
            },
        ]
        judge_response = json.dumps(
            {
                "drop_ids": ["stale-event-index"],
                "reason_by_id": {"stale-event-index": "低价值"},
                "notes": "只删除 memU 索引副本",
            },
            ensure_ascii=False,
        )

        with patch("pupu.memory_index.memu_tidy.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_tidy._list_items", return_value=items):
                with patch("pupu.memory_index.memu_tidy._get_service", return_value=FakeService()):
                    with patch(
                        "pupu.memory_index.memu_tidy._sync_event_threads_after_tidy",
                        return_value={"status": "ok", "checked": 0, "missing": 0, "synced": 0},
                    ):
                        with patch("pupu.memory_index.memu_tidy.json_task", return_value=judge_response):
                            result = run_memu_tidy(self.session_id, mode="apply")

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["source_deleted"], 0)
        self.assertEqual(result["local_deleted"], 0)
        self.assertEqual(deleted_ids, ["stale-event-index"])

    def legacy_memu_tidy_ignores_unsupported_model_operations(self):
        deleted_ids = []

        class FakeService:
            async def delete_memory_item(self, *, memory_id, user=None):
                deleted_ids.append(memory_id)
                return {"id": memory_id}

        items = [
            {
                "id": "junk-fact",
                "summary": '{"kind":"person_fact","key":"temp_note","text":"小夫 | temp_note: True","created_at":"2026-05-11T12:00:00"}',
                "memory_type": "profile",
            }
        ]
        judge_response = json.dumps(
            {
                "drop_ids": ["junk-fact"],
                "reason_by_id": {"junk-fact": "低价值"},
                "delete_source": True,
                "operations": [{"action": "delete_local_fact", "key": "temp_note"}],
            },
            ensure_ascii=False,
        )

        with patch("pupu.memory_index.memu_tidy.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_tidy._list_items", return_value=items):
                with patch("pupu.memory_index.memu_tidy._get_service", return_value=FakeService()):
                    with patch(
                        "pupu.memory_index.memu_tidy._sync_event_threads_after_tidy",
                        return_value={"status": "ok", "checked": 0, "missing": 0, "synced": 0},
                    ):
                        with patch("pupu.memory_index.memu_tidy.json_task", return_value=judge_response):
                            result = run_memu_tidy(self.session_id, mode="apply")

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["source_deleted"], 0)
        self.assertEqual(result["local_deleted"], 0)
        self.assertEqual(deleted_ids, ["junk-fact"])

    def legacy_memu_tidy_check_does_not_delete(self):
        items = [
            {
                "id": "junk-fact",
                "summary": '{"kind":"person_fact","key":"temp_note","text":"小夫 | temp_note: True","created_at":"2026-05-11T12:00:00"}',
                "memory_type": "profile",
            }
        ]
        judge_response = json.dumps(
            {
                "drop_ids": ["junk-fact"],
                "reason_by_id": {"junk-fact": "低价值"},
                "notes": "只预览",
            },
            ensure_ascii=False,
        )

        with patch("pupu.memory_index.memu_tidy.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_tidy._list_items", return_value=items):
                with patch("pupu.memory_index.memu_tidy._get_service") as mock_get_service:
                    with patch(
                        "pupu.memory_index.memu_tidy._check_event_thread_sync",
                        return_value={"status": "ok", "checked": 0, "missing": 0, "synced": 0},
                    ):
                        with patch("pupu.memory_index.memu_tidy.json_task", return_value=judge_response):
                            result = run_memu_tidy(self.session_id, mode="check")

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["source_deleted"], 0)
        self.assertEqual(result["local_deleted"], 0)
        mock_get_service.assert_not_called()

    def legacy_memu_tidy_check_reports_missing_source_sync_without_writing(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "missing-source-event",
                    "title": "Missing source event",
                    "kind": "milestone",
                    "details": "not in memU yet",
                    "confidence": 1.0,
                    "status": "active",
                }
            ],
        )

        with patch("pupu.memory_index.memu_tidy.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_tidy._list_items", return_value=[]):
                with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
                    with patch("pupu.memory_index.memu_adapter._list_items", return_value=[]):
                        with patch("pupu.memory_index.memu_adapter.sync_review_memory") as mock_sync:
                            with patch("pupu.memory_index.memu_tidy.json_task", return_value='{"drop_ids":[]}'):
                                result = run_memu_tidy(self.session_id, mode="check")

        self.assertEqual(result["source_sync"]["status"], "missing")
        self.assertEqual(result["source_sync"]["checked"], 1)
        self.assertEqual(result["source_sync"]["missing"], 1)
        self.assertEqual(result["source_sync"]["synced"], 0)
        mock_sync.assert_not_called()

    def legacy_memu_tidy_apply_backfills_missing_source_events_after_cleanup(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "missing-source-event",
                    "title": "Missing source event",
                    "kind": "milestone",
                    "details": "not in memU yet",
                    "confidence": 1.0,
                    "status": "active",
                }
            ],
        )

        with patch("pupu.memory_index.memu_tidy.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_tidy._list_items", return_value=[]):
                with patch("pupu.memory_index.memu_tidy._get_service"):
                    with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
                        with patch("pupu.memory_index.memu_adapter._list_items", return_value=[]):
                            with patch(
                                "pupu.memory_index.memu_adapter.sync_review_memory",
                                return_value=MemuWriteResult(status="success", ids=["synced-event"]),
                            ) as mock_sync:
                                with patch("pupu.memory_index.memu_tidy.json_task", return_value='{"drop_ids":[]}'):
                                    result = run_memu_tidy(self.session_id, mode="apply")

        self.assertEqual(result["source_sync"]["status"], "synced")
        self.assertEqual(result["source_sync"]["checked"], 1)
        self.assertEqual(result["source_sync"]["missing"], 1)
        self.assertEqual(result["source_sync"]["synced"], 1)
        sync_kwargs = mock_sync.call_args.kwargs
        self.assertEqual(sync_kwargs["event_threads"][0]["thread_key"], "missing-source-event")

    def test_memu_tidy_apply_creates_missing_source_cards(self):
        created = []

        class FakeService:
            async def list_memory_items(self, *, where=None):
                return {"items": []}

            async def create_memory_item(self, **kwargs):
                created.append(kwargs)
                return {"memory_item": {"id": f"m{len(created)}"}}

        upsert_person_facts(
            {"game": "pixel farm"},
            default_subject_person_key=person_from_session(self.session_id),
        )
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "cache-event",
                    "title": "Cache event",
                    "kind": "milestone",
                    "details": "cache me",
                    "confidence": 1.0,
                    "status": "active",
                },
            ],
        )

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                result = run_memu_tidy(self.session_id, mode="apply")

        self.assertEqual(result["status"], "synced")
        self.assertGreaterEqual(result["missing"], 2)
        self.assertGreaterEqual(result["created"], 2)
        payloads = [json.loads(item["memory_content"]) for item in created]
        self.assertTrue({"person_fact", "event_thread"}.issubset({payload["source_type"] for payload in payloads}))
        self.assertTrue(all(payload["projection_kind"] == "rag_card" for payload in payloads))

    def test_memu_tidy_apply_deletes_orphan_and_duplicate_source_cards(self):
        deleted_ids = []
        created = []

        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "kept-event",
                    "title": "Kept event",
                    "kind": "milestone",
                    "details": "keep me",
                    "confidence": 1.0,
                    "status": "active",
                },
            ],
        )
        source_key = f"event_thread:{self.session_id}:kept-event"
        items = [
            {
                "id": "kept-1",
                "summary": json.dumps(
                    {
                        "kind": "event_thread",
                        "text": "old kept",
                        "projection_kind": "rag_card",
                        "source_type": "event_thread",
                        "source_key": source_key,
                        "source_version": "old",
                    }
                ),
            },
            {
                "id": "kept-dup",
                "summary": json.dumps(
                    {
                        "kind": "event_thread",
                        "text": "duplicate",
                        "projection_kind": "rag_card",
                        "source_type": "event_thread",
                        "source_key": source_key,
                        "source_version": "old",
                    }
                ),
            },
            {
                "id": "orphan",
                "summary": json.dumps(
                    {
                        "kind": "event_thread",
                        "text": "orphan",
                        "projection_kind": "rag_card",
                        "source_type": "event_thread",
                        "source_key": "event_thread:missing:missing",
                        "source_version": "old",
                    }
                ),
            },
        ]

        class FakeService:
            async def list_memory_items(self, *, where=None):
                return {"items": items}

            async def delete_memory_item(self, *, memory_id, user=None):
                deleted_ids.append(memory_id)
                return {"id": memory_id}

            async def create_memory_item(self, **kwargs):
                created.append(kwargs)
                return {"memory_item": {"id": f"created-{len(created)}"}}

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                result = run_memu_tidy(self.session_id, mode="apply")

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["orphaned"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(set(deleted_ids), {"kept-1", "kept-dup", "orphan"})
        self.assertGreaterEqual(len(created), 1)

    def test_memu_tidy_check_reports_drift_without_writing(self):
        class FakeService:
            async def list_memory_items(self, *, where=None):
                return {"items": []}

            async def create_memory_item(self, **kwargs):
                raise AssertionError("check mode must not write")

            async def delete_memory_item(self, **kwargs):
                raise AssertionError("check mode must not delete")

        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "missing-source-event",
                    "title": "Missing source event",
                    "kind": "milestone",
                    "details": "not in memU yet",
                    "confidence": 1.0,
                    "status": "active",
                }
            ],
        )

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                result = run_memu_tidy(self.session_id, mode="check")

        self.assertEqual(result["status"], "drift")
        self.assertGreaterEqual(result["checked"], 1)
        self.assertGreaterEqual(result["missing"], 1)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["deleted"], 0)

    def test_memu_tidy_rebuild_clears_old_cache_and_recreates_sources(self):
        created = []
        cleared = []

        class FakeService:
            async def list_memory_items(self, *, where=None):
                return {
                    "items": [
                        {
                            "id": "old",
                            "summary": '{"kind":"person_fact","text":"old"}',
                        }
                    ]
                }

            async def clear_memory(self, *, where=None):
                cleared.append(where)
                return {"deleted_items": [{"id": "old"}]}

            async def create_memory_item(self, **kwargs):
                created.append(kwargs)
                return {"memory_item": {"id": f"new-{len(created)}"}}

        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "missing-source-event",
                    "title": "Missing source event",
                    "kind": "milestone",
                    "details": "not in memU yet",
                    "confidence": 1.0,
                    "status": "active",
                }
            ],
        )

        with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
            with patch("pupu.memory_index.memu_adapter._get_service", return_value=FakeService()):
                result = run_memu_tidy(self.session_id, mode="rebuild")

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["deleted"], 1)
        self.assertGreaterEqual(result["created"], 1)
        self.assertEqual(cleared, [{}])
        payloads = [json.loads(item["memory_content"]) for item in created]
        self.assertTrue(any(payload["source_type"] == "event_thread" for payload in payloads))

if __name__ == "__main__":
    unittest.main()
