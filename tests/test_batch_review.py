import os
from pathlib import Path
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.agent import (
    _format_chat_history_for_prompt,
    _format_event_thread_candidates_for_review,
    _format_message_content_for_prompt,
    _format_turn_timestamp,
    _parse_batch_review_result,
)
from pupu.memory import (
    append_event_step,
    _get_conn,
    create_scheduled_task,
    get_familiarity,
    get_event_threads,
    get_pending_review_last_message_time,
    get_review_candidate_batch,
    get_summaries,
    get_summary_trigger_progress,
    init_db,
    list_pending_review_sessions,
    list_scheduled_tasks,
    reset_session,
    save_message,
    save_message_with_speaker,
    save_summary,
    set_familiarity,
    update_familiarity,
    upsert_event_threads,
)
from pupu.storage import get_conn, upsert_person
from pupu.persona import build_batch_review_prompt

from pupu.message_sources import CHAT, PROACTIVE, SCHEDULED


class BatchReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_batch_review_{self._testMethodName}"
        reset_session(self.session_id)
        conn = get_conn()
        try:
            conn.execute("DELETE FROM people")
            conn.commit()
        finally:
            conn.close()

    def _save_chat_turn(self, index: int):
        save_message("user", f"user-{index}", self.session_id, source=CHAT)
        save_message("assistant", f"assistant-{index}", self.session_id, source=CHAT)

    def test_batch_review_prompt_requires_concrete_event_memory(self):
        prompt = build_batch_review_prompt()

        self.assertIn("谁在什么时间/场景做了什么", prompt)
        self.assertIn("关系升温、进行了亲密互动、氛围很好", prompt)
        self.assertIn("summary、facts、title、time_text", prompt)
        self.assertIn("2026年5月19日这轮对话中", prompt)
        self.assertIn("event_updates 用于维护“持续事件线”", prompt)

    def test_batch_review_prompt_uses_instance_name_for_subject_rules(self):
        prompt = build_batch_review_prompt(character_name="璐璐")

        self.assertIn("你是璐璐的记忆整理器", prompt)
        self.assertIn("人物名：发言 <end>", prompt)
        self.assertIn("不要泛化成“用户”“实例”“双方”", prompt)
        self.assertIn("不要输出 QQ 号、person_key、qq:xxx", prompt)
        self.assertIn("不要把璐璐写成“仆仆”", prompt)
        self.assertNotIn("不要把璐璐写成“璐璐”", prompt)

    def test_turn_timestamp_includes_weekday(self):
        class FixedDateTime:
            @classmethod
            def now(cls):
                from datetime import datetime

                return datetime(2026, 6, 18, 19, 42)

        with patch("pupu.agent.datetime", FixedDateTime):
            self.assertEqual(_format_turn_timestamp(), "2026-06-18 周四 19:42")

    def test_summary_progress_counts_chat_messages(self):
        for i in range(3):
            self._save_chat_turn(i)

        save_message("assistant", "proactive ping", self.session_id, source=PROACTIVE)
        save_message("user", "scheduled user", self.session_id, source=SCHEDULED)
        save_message(
            "assistant",
            "scheduled assistant",
            self.session_id,
            source=SCHEDULED,
        )

        progress = get_summary_trigger_progress(self.session_id, review_interval=8)

        self.assertEqual(progress["pending"], 6)
        self.assertEqual(progress["remaining"], 2)
        self.assertFalse(progress["ready"])

    def test_review_candidate_batch_uses_message_count_and_skips_internal_sources(self):
        for i in range(10):
            self._save_chat_turn(i)
            if i == 2:
                save_message(
                    "assistant",
                    "proactive ping",
                    self.session_id,
                    source=PROACTIVE,
                )

        batch = get_review_candidate_batch(
            session_id=self.session_id,
            review_interval=8,
            source=CHAT,
        )

        self.assertEqual(len(batch), 8)
        self.assertEqual(sum(1 for item in batch if item["role"] == "assistant"), 4)
        self.assertTrue(batch)
        self.assertTrue(all(item["source"] == CHAT for item in batch))
        self.assertEqual(batch[0]["content"], "user-0")
        self.assertEqual(batch[-1]["content"], "assistant-3")

    def test_saved_summary_advances_review_cursor_by_batch_end(self):
        for i in range(4):
            self._save_chat_turn(i)

        batch = get_review_candidate_batch(
            session_id=self.session_id,
            review_interval=8,
            source=CHAT,
        )
        save_summary("batch one", batch[0]["id"], batch[-1]["id"], self.session_id)

        progress = get_summary_trigger_progress(self.session_id, review_interval=8)
        next_batch = get_review_candidate_batch(
            session_id=self.session_id,
            review_interval=8,
            source=CHAT,
        )

        self.assertEqual(progress["pending"], 0)
        self.assertEqual(progress["remaining"], 8)
        self.assertEqual(next_batch, [])

    def test_pending_review_last_message_time_uses_unsummarized_chat_only(self):
        for i in range(2):
            self._save_chat_turn(i)
        save_message("assistant", "proactive", self.session_id, source=PROACTIVE)

        conn = _get_conn()
        try:
            chat_time = "2026-04-26T10:00:00"
            proactive_time = "2026-04-26T11:00:00"
            conn.execute(
                "UPDATE messages SET timestamp = ? WHERE session_id = ? AND source = 'chat'",
                (chat_time, self.session_id),
            )
            conn.execute(
                "UPDATE messages SET timestamp = ? WHERE session_id = ? AND source = 'proactive'",
                (proactive_time, self.session_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(
            get_pending_review_last_message_time(self.session_id, source=CHAT),
            chat_time,
        )

    def test_parse_batch_review_result_handles_fences_and_trailing_commas(self):
        raw = """```json
{
  "summary": "talked about movies",
  "familiarity_delta": 2,
  "person_facts": [{"subject": "小夫", "scope": "person", "key": "favorite_genre", "value": "fantasy",},],
  "event_updates": [{
    "action": "create_thread",
    "thread_key": "birthday-2026-04-27",
    "title": "user birthday tomorrow",
    "kind": "birthday",
    "event_time": "2026-04-27",
    "time_text": "tomorrow",
    "summary": "user said birthday tomorrow",
    "followup_hint": "wish happy birthday",
    "confidence": 0.9
  },],
  "task_updates": [{
    "action": "create",
    "thread_key": "birthday-2026-04-27",
    "title": "birthday wish",
    "instruction": "wish happy birthday",
    "run_at": "2026-04-27T09:00:00",
    "repeat": "once",
    "interval_seconds": null
  },]
}
```"""

        parsed = _parse_batch_review_result(raw)

        self.assertEqual(parsed["summary"], "talked about movies")
        self.assertEqual(parsed["familiarity_delta"], 2)
        self.assertEqual(parsed["person_facts"][0]["key"], "favorite_genre")
        self.assertEqual(parsed["fact_updates"][0]["action"], "create")
        self.assertEqual(parsed["fact_updates"][0]["key"], "favorite_genre")
        self.assertEqual(
            parsed["event_updates"][0]["thread_key"],
            "birthday-2026-04-27",
        )
        self.assertEqual(parsed["task_updates"][0]["action"], "create")

    def test_parse_batch_review_result_repairs_unescaped_quotes_inside_strings(self):
        raw = """```json
{
  "summary": "用户用"永远在一起"表达想做一辈子朋友。",
  "familiarity_delta": 8,
  "person_facts": [
    {"subject": "小夫", "scope": "person", "key": "commitment", "value": "用户承诺要做仆仆一辈子的朋友，永远在一起"}
  ],
  "event_updates": [
    {
      "action": "create_thread",
      "thread_key": "eternal_friendship_promise",
      "title": "永远的朋友承诺",
      "kind": "promise",
      "event_time": "2026-04-26",
      "time_text": "今天",
      "summary": "用户和仆仆确立了永远做朋友的承诺。",
      "followup_hint": "这是关系的重要里程碑",
      "confidence": 0.95
    }
  ],
  "task_updates": []
}
```"""

        parsed = _parse_batch_review_result(raw)

        self.assertIn('"永远在一起"', parsed["summary"])
        self.assertEqual(parsed["familiarity_delta"], 8)
        self.assertEqual(
            parsed["event_updates"][0]["thread_key"],
            "eternal_friendship_promise",
        )

    def test_parse_batch_review_result_filters_structured_fact_values(self):
        raw = """{
          "summary": "整理事实。",
          "familiarity_delta": 0,
          "person_facts": [
            {"subject": "小夫", "scope": "person", "key": "爱好", "value": ["画画"]},
            {"subject": "小夫", "scope": "person", "key": "昵称", "value": "小夫"},
            {"subject": "璐璐", "scope": "person", "key": "会做饭", "value": true}
          ],
          "event_updates": [],
          "task_updates": []
        }"""

        parsed = _parse_batch_review_result(raw)

        self.assertEqual(
            parsed["person_facts"],
            [
                {
                    "subject": "小夫",
                    "object": "",
                    "scope": "person",
                    "key": "昵称",
                    "value": "小夫",
                    "confidence": 1.0,
                }
            ],
        )
        self.assertEqual(parsed["fact_updates"][0]["action"], "create")
        self.assertEqual(parsed["fact_updates"][0]["key"], "昵称")

    def test_parse_batch_review_result_accepts_fact_updates(self):
        raw = """{
          "summary": "整理事实。",
          "familiarity_delta": 0,
          "fact_updates": [
            {"action": "update_existing", "fact_id": 12, "value": "小夫是光头，没有刘海", "confidence": 0.9},
            {"action": "create", "subject": "小夫", "scope": "person", "key": "近况", "value": "小夫在调整记忆系统"}
          ],
          "event_updates": [],
          "task_updates": []
        }"""

        parsed = _parse_batch_review_result(raw)

        self.assertEqual(
            parsed["fact_updates"],
            [
                {
                    "action": "update_existing",
                    "fact_id": 12,
                    "value": "小夫是光头，没有刘海",
                    "confidence": 0.9,
                },
                {
                    "action": "create",
                    "subject": "小夫",
                    "object": "",
                    "scope": "person",
                    "key": "近况",
                    "value": "小夫在调整记忆系统",
                    "confidence": 1.0,
                },
            ],
        )

    def test_familiarity_updates_accumulate_in_order(self):
        set_familiarity(0, session_id=self.session_id)
        self.assertEqual(get_familiarity(self.session_id), 0)

        update_familiarity(2, session_id=self.session_id)
        update_familiarity(3, session_id=self.session_id)

        self.assertEqual(get_familiarity(self.session_id), 5)

    def test_familiarity_updates_do_not_write_legacy_event_log_by_default(self):
        update_familiarity(4, session_id=self.session_id)

        conn = _get_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(count, 0)

    def test_batch_review_uses_json_task_provider_output(self):
        for i in range(10):
            self._save_chat_turn(i)
        create_scheduled_task(
            self.session_id,
            "睡觉提醒",
            "提醒用户睡觉",
            "2026-04-26T23:00:00",
            "once",
            None,
        )

        raw = """{
          "summary": "用户和仆仆聊了一个重要约定。",
          "familiarity_delta": 1,
          "event_updates": [{
            "action": "create_thread",
            "thread_key": "promise-test",
            "title": "测试约定",
            "kind": "promise",
            "event_time": "",
            "time_text": "刚才",
            "summary": "用户和仆仆说好要记住这件事。",
            "followup_hint": "之后自然提起",
            "confidence": 0.8
          }],
          "task_updates": [{
            "action": "cancel_matching",
            "query": "睡觉提醒",
            "reason": "用户已经准备睡觉"
          }]
        }"""

        from pupu.agent import _maybe_batch_review

        set_familiarity(0, session_id=self.session_id)
        with patch("pupu.agent.json_task", return_value=raw) as mock_json_task:
            _maybe_batch_review(self.session_id)

        summaries = get_summaries(self.session_id, limit=3)
        events = get_event_threads(self.session_id, limit=3)
        review_input = mock_json_task.call_args.kwargs["user_content"]

        mock_json_task.assert_called_once()
        self.assertIn("当前已有定时任务", review_input)
        self.assertIn("不要使用 id", review_input)
        self.assertIn("睡觉提醒", review_input)
        self.assertIn("提醒用户睡觉", review_input)
        self.assertIn("2026-04-26T23:00:00", review_input)
        self.assertIn("待整理对话", review_input)
        self.assertEqual(get_familiarity(self.session_id), 1)
        self.assertEqual(summaries[-1]["summary"], "用户和仆仆聊了一个重要约定。")
        self.assertEqual(events[0]["thread_key"], "promise-test")
        self.assertEqual(list_scheduled_tasks(self.session_id), [])

    def test_batch_review_event_candidates_include_recent_steps(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "cake-check",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让仆仆验收",
                    "merge_hint": "草莓蛋糕 验收 大颗草莓",
                    "confidence": 0.95,
                }
            ],
        )
        append_event_step(
            self.session_id,
            "cake-check",
            step_type="instance",
            summary="仆仆提醒用户晚上要验收草莓蛋糕",
            cause="仆仆主动跟进约定",
            reflection="这次提醒让约定更清晰",
        )

        text = _format_event_thread_candidates_for_review(
            self.session_id,
            "今天要检查草莓蛋糕",
        )

        self.assertIn("thread_key=cake-check", text)
        self.assertIn("recent_step[instance]", text)
        self.assertIn("仆仆主动跟进约定", text)
        self.assertIn("这次提醒让约定更清晰", text)

    def test_batch_review_input_uses_instance_name_instead_of_pupu(self):
        for i in range(10):
            self._save_chat_turn(i)

        raw = """{
          "summary": "2026年5月21日，璐璐说自己想买二手屏。",
          "familiarity_delta": 0,
          "event_updates": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review

        with (
            patch("pupu.agent.get_pupu_name", return_value="璐璐"),
            patch("pupu.agent.json_task", return_value=raw) as mock_json_task,
        ):
            _maybe_batch_review(self.session_id)

        review_input = mock_json_task.call_args.kwargs["user_content"]
        review_system = mock_json_task.call_args.kwargs["system"]

        self.assertIn("用户：user-0 <end>", review_input)
        self.assertIn("璐璐：assistant-0 <end>", review_input)
        self.assertNotIn("Current participants", review_input)
        self.assertNotIn("Pupu:", review_input)
        self.assertIn("你是璐璐的记忆整理器", review_system)
        self.assertIn("不要把璐璐写成“仆仆”", review_system)

    def test_batch_review_input_uses_fixed_person_display_names(self):
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "owner",
                kind="owner",
                display_name="小夫",
                qq_id="424225912",
                aliases=["用户"],
            )
            conn.commit()
        finally:
            conn.close()

        for i in range(5):
            save_message_with_speaker(
                "user",
                f"user-{i}",
                self.session_id,
                source=CHAT,
                speaker_key="owner",
                speaker_name="群昵称会变",
                speaker_qq="424225912",
            )
            save_message_with_speaker(
                "assistant",
                f"assistant-{i}",
                self.session_id,
                source=CHAT,
                speaker_key="instance",
                speaker_name="璐璐",
            )

        raw = """{
          "summary": "小夫和璐璐完成一轮测试对话。",
          "familiarity_delta": 0,
          "event_updates": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review

        with (
            patch("pupu.agent.get_pupu_name", return_value="璐璐"),
            patch("pupu.agent.json_task", return_value=raw) as mock_json_task,
        ):
            _maybe_batch_review(self.session_id)

        review_input = mock_json_task.call_args.kwargs["user_content"]

        self.assertIn("小夫：user-0 <end>", review_input)
        self.assertIn("璐璐：assistant-0 <end>", review_input)
        self.assertNotIn("群昵称会变：", review_input)
        self.assertNotIn("Current participants", review_input)
        self.assertNotIn("424225912", review_input)
        self.assertNotIn("qq:", review_input)

    def test_batch_review_strips_open_group_qq_prefixes(self):
        payload = (
            '[{"person_key":"qq:123","display_name":"Alice","qq_id":"123","kind":"qq"},'
            '{"person_key":"qq:456","display_name":"Bob","qq_id":"456","kind":"qq"}]'
        )
        save_message_with_speaker(
            "user",
            "[Alice(QQ:123)] hi\n[Bob(QQ:456)] hello",
            self.session_id,
            source=CHAT,
            speaker_key=payload,
            speaker_name="Bob",
            speaker_qq="456",
        )
        for i in range(9):
            save_message_with_speaker(
                "assistant",
                f"assistant-{i}",
                self.session_id,
                source=CHAT,
                speaker_key="instance",
                speaker_name="璐璐",
            )

        raw = """{
          "summary": "Alice、Bob 和璐璐完成群聊测试。",
          "familiarity_delta": 0,
          "event_updates": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review

        with (
            patch("pupu.agent.get_pupu_name", return_value="璐璐"),
            patch("pupu.agent.json_task", return_value=raw) as mock_json_task,
        ):
            _maybe_batch_review(self.session_id)

        review_input = mock_json_task.call_args.kwargs["user_content"]

        self.assertIn("Alice：hi <end>", review_input)
        self.assertIn("Bob：hello <end>", review_input)
        self.assertNotIn("QQ:123", review_input)
        self.assertNotIn("qq:123", review_input)

    def test_live_prompt_uses_fixed_person_names_for_group_prefixes(self):
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "owner",
                kind="owner",
                display_name="小夫",
                qq_id="424225912",
                aliases=["钮钴禄·大家大宁"],
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
        message = {
            "role": "user",
            "content": "[钮钴禄·大家大宁(QQ:424225912)] 大家都是我老婆\n"
            "[仆仆(QQ:3853876778)] 你想得挺美",
            "speaker_key": payload,
            "speaker_name": "钮钴禄·大家大宁",
            "speaker_qq": "424225912",
        }
        people = [
            {
                "person_key": "owner",
                "display_name": "小夫",
                "qq_id": "424225912",
                "kind": "owner",
            },
            {
                "person_key": "qq:3853876778",
                "display_name": "仆仆",
                "qq_id": "3853876778",
                "kind": "qq",
            },
        ]

        content = _format_message_content_for_prompt(
            message,
            character_name="璐璐",
            people=people,
        )

        self.assertIn("小夫：大家都是我老婆", content)
        self.assertIn("仆仆：你想得挺美", content)
        self.assertNotIn("大宁", content)
        self.assertNotIn("QQ:424225912", content)

    def test_live_chat_history_uses_fixed_person_names(self):
        payload = (
            '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"}]'
        )
        history = [
            {
                "role": "user",
                "content": "[钮钴禄·大家大宁(QQ:424225912)] 干嘛啦",
                "speaker_key": payload,
                "speaker_name": "钮钴禄·大家大宁",
                "speaker_qq": "424225912",
            },
            {
                "role": "assistant",
                "content": "别在群里闹",
                "speaker_key": "instance",
                "speaker_name": "璐璐",
                "speaker_qq": "",
            },
        ]
        people = [
            {
                "person_key": "owner",
                "display_name": "小夫",
                "qq_id": "424225912",
                "kind": "owner",
            },
            {
                "person_key": "instance",
                "display_name": "璐璐",
                "qq_id": "",
                "kind": "instance",
            },
        ]

        messages = _format_chat_history_for_prompt(
            history,
            character_name="璐璐",
            people=people,
        )

        self.assertEqual(messages[0]["content"], "小夫：干嘛啦")
        self.assertEqual(messages[1]["content"], "璐璐：别在群里闹")
        self.assertNotIn("钮钴禄", "\n".join(item["content"] for item in messages))

    def test_group_chat_history_can_render_assistant_as_bare_reply(self):
        history = [
            {
                "role": "assistant",
                "content": "璐璐：别在群里闹",
                "speaker_key": "instance",
                "speaker_name": "璐璐",
                "speaker_qq": "",
            },
        ]
        people = [
            {
                "person_key": "instance",
                "display_name": "璐璐",
                "qq_id": "",
                "kind": "instance",
            },
        ]

        messages = _format_chat_history_for_prompt(
            history,
            character_name="璐璐",
            people=people,
            bare_assistant=True,
        )

        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["content"], "别在群里闹")

    def test_live_chat_history_places_timestamp_before_speaker_name(self):
        payload = (
            '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"}]'
        )
        history = [
            {
                "role": "user",
                "content": "[时间: 2026-06-19 周五 00:12] 干嘛啦",
                "speaker_key": payload,
                "speaker_name": "钮钴禄·大家大宁",
                "speaker_qq": "424225912",
            },
        ]
        people = [
            {
                "person_key": "owner",
                "display_name": "小夫",
                "qq_id": "424225912",
                "kind": "owner",
            },
        ]

        messages = _format_chat_history_for_prompt(
            history,
            character_name="璐璐",
            people=people,
        )

        self.assertEqual(messages[0]["content"], "[时间: 2026-06-19 周五 00:12] 小夫：干嘛啦")
        self.assertNotIn("小夫：[时间:", messages[0]["content"])

    def test_live_chat_history_strips_duplicate_speaker_prefixes(self):
        history = [
            {
                "role": "assistant",
                "content": "璐璐：真睡了？",
                "speaker_key": "instance",
                "speaker_name": "璐璐",
                "speaker_qq": "",
            },
        ]
        people = [
            {
                "person_key": "instance",
                "display_name": "璐璐",
                "qq_id": "",
                "kind": "instance",
            },
        ]

        messages = _format_chat_history_for_prompt(
            history,
            character_name="璐璐",
            people=people,
        )

        self.assertEqual(messages[0]["content"], "璐璐：真睡了？")

    def test_prefixed_group_lines_strip_duplicate_speaker_prefixes(self):
        payload = (
            '[{"person_key":"qq:3853876778","display_name":"仆仆","qq_id":"3853876778","kind":"qq"}]'
        )
        message = {
            "role": "user",
            "content": "[仆仆(QQ:3853876778)] 仆仆：又来了\n"
            "仆仆：我煮粽子去",
            "speaker_key": payload,
            "speaker_name": "仆仆",
            "speaker_qq": "3853876778",
        }
        people = [
            {
                "person_key": "qq:3853876778",
                "display_name": "仆仆",
                "qq_id": "3853876778",
                "kind": "qq",
            },
        ]

        content = _format_message_content_for_prompt(
            message,
            character_name="璐璐",
            people=people,
        )

        self.assertIn("仆仆：又来了", content)
        self.assertIn("仆仆：我煮粽子去", content)
        self.assertNotIn("仆仆：仆仆：", content)

    def test_prefixed_group_lines_place_timestamp_before_each_speaker(self):
        payload = (
            '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"},'
            '{"person_key":"qq:3853876778","display_name":"仆仆","qq_id":"3853876778","kind":"qq"}]'
        )
        message = {
            "role": "user",
            "content": "[时间: 2026-06-19 周五 00:12] [钮钴禄·大家大宁(QQ:424225912)] 姐姐们\n"
            "[仆仆(QQ:3853876778)] 又来了",
            "speaker_key": payload,
            "speaker_name": "钮钴禄·大家大宁",
            "speaker_qq": "424225912",
        }
        people = [
            {
                "person_key": "owner",
                "display_name": "小夫",
                "qq_id": "424225912",
                "kind": "owner",
            },
            {
                "person_key": "qq:3853876778",
                "display_name": "仆仆",
                "qq_id": "3853876778",
                "kind": "qq",
            },
        ]

        content = _format_message_content_for_prompt(
            message,
            character_name="璐璐",
            people=people,
        )

        self.assertIn("[时间: 2026-06-19 周五 00:12] 小夫：姐姐们", content)
        self.assertIn("[时间: 2026-06-19 周五 00:12] 仆仆：又来了", content)
        self.assertNotIn("小夫：[时间:", content)
        self.assertNotIn("仆仆：[时间:", content)

    def test_live_group_chat_uses_plain_names_without_relationship_prefixes(self):
        set_familiarity(100, session_id="owner")
        set_familiarity(50, session_id="private_3853876778")
        payload = (
            '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"},'
            '{"person_key":"qq:3853876778","display_name":"仆仆","qq_id":"3853876778","kind":"qq"},'
            '{"person_key":"instance","display_name":"璐璐","qq_id":"","kind":"instance"}]'
        )
        message = {
            "role": "user",
            "content": "[时间: 2026-06-19 周五 08:10] [钮钴禄·大家大宁(QQ:424225912)] 姐姐们\n"
            "[仆仆(QQ:3853876778)] 仆仆：又来了",
            "speaker_key": payload,
            "speaker_name": "钮钴禄·大家大宁",
            "speaker_qq": "424225912",
        }
        people = [
            {
                "person_key": "owner",
                "display_name": "小夫",
                "qq_id": "424225912",
                "kind": "owner",
            },
            {
                "person_key": "qq:3853876778",
                "display_name": "仆仆",
                "qq_id": "3853876778",
                "kind": "qq",
            },
            {
                "person_key": "instance",
                "display_name": "璐璐",
                "qq_id": "",
                "kind": "instance",
            },
        ]

        content = _format_message_content_for_prompt(
            message,
            character_name="璐璐",
            people=people,
        )

        self.assertIn("[时间: 2026-06-19 周五 08:10] 小夫：姐姐们", content)
        self.assertIn("[时间: 2026-06-19 周五 08:10] 仆仆：又来了", content)
        self.assertNotIn("“恋人”", content)
        self.assertNotIn("“朋友”", content)
        self.assertNotIn("仆仆：仆仆：", content)

        self_content = _format_message_content_for_prompt(
            {
                "role": "assistant",
                "content": "我在",
                "speaker_key": "instance",
                "speaker_name": "璐璐",
                "speaker_qq": "",
            },
            character_name="璐璐",
            people=people,
            bare_assistant=True,
        )
        self.assertEqual(self_content, "我在")

    def test_batch_review_keeps_plain_speaker_names_without_relationship_prefixes(self):
        payload = (
            '[{"person_key":"owner","display_name":"钮钴禄·大家大宁","qq_id":"424225912","kind":"owner"},'
            '{"person_key":"qq:3853876778","display_name":"仆仆","qq_id":"3853876778","kind":"qq"}]'
        )
        message = {
            "role": "user",
            "content": "[时间: 2026-06-19 周五 08:10] [钮钴禄·大家大宁(QQ:424225912)] 姐姐们\n"
            "[仆仆(QQ:3853876778)] 又来了",
            "speaker_key": payload,
            "speaker_name": "钮钴禄·大家大宁",
            "speaker_qq": "424225912",
        }
        people = [
            {
                "person_key": "owner",
                "display_name": "小夫",
                "qq_id": "424225912",
                "kind": "owner",
            },
            {
                "person_key": "qq:3853876778",
                "display_name": "仆仆",
                "qq_id": "3853876778",
                "kind": "qq",
            },
        ]

        content = _format_message_content_for_prompt(
            message,
            character_name="璐璐",
            people=people,
        )

        self.assertIn("[时间: 2026-06-19 周五 08:10] 小夫：姐姐们", content)
        self.assertIn("[时间: 2026-06-19 周五 08:10] 仆仆：又来了", content)
        self.assertNotIn("“恋人”", content)
        self.assertNotIn("“朋友”", content)

    def test_group_relationship_prefix_does_not_create_default_familiarity(self):
        reset_session("private_999001")
        payload = (
            '[{"person_key":"qq:999001","display_name":"新群友","qq_id":"999001","kind":"qq"}]'
        )
        message = {
            "role": "user",
            "content": "[新群友(QQ:999001)] 第一次见",
            "speaker_key": payload,
            "speaker_name": "新群友",
            "speaker_qq": "999001",
        }
        people = [
            {
                "person_key": "qq:999001",
                "display_name": "新群友",
                "qq_id": "999001",
                "kind": "qq",
            },
        ]

        content = _format_message_content_for_prompt(
            message,
            character_name="璐璐",
            people=people,
            include_relationship_prefix=True,
        )

        self.assertEqual(content, "“朋友”新群友：第一次见")
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT session_id FROM familiarity WHERE session_id = ?",
                ("private_999001",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_batch_review_splits_context_summary_and_identity_memory(self):
        context_id = self.session_id + "_context"
        identity_id = self.session_id + "_identity"
        reset_session(context_id)
        reset_session(identity_id)
        set_familiarity(0, session_id=identity_id)
        for i in range(10):
            save_message("user", f"user-{i}", context_id, source=CHAT)
            save_message("assistant", f"assistant-{i}", context_id, source=CHAT)

        raw = """{
          "summary": "群上下文摘要。",
          "familiarity_delta": 2,
          "person_facts": [{"subject": "__IDENTITY__", "scope": "person", "key": "喜欢", "value": "草莓"}],
          "event_updates": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review
        from pupu.memory import get_person_facts

        with patch("pupu.agent.json_task", return_value=raw.replace("__IDENTITY__", identity_id)):
            _maybe_batch_review(context_id, identity_session=identity_id)

        self.assertEqual(get_summaries(context_id, limit=1)[-1]["summary"], "群上下文摘要。")
        self.assertEqual(get_familiarity(identity_id), 2)
        facts = get_person_facts(subject_person_keys=[identity_id], include_relationships=False)
        self.assertEqual(
            {(row["subject_person_key"], row["fact_key"]): row["fact_value"] for row in facts},
            {(identity_id, "喜欢"): "草莓"},
        )
        self.assertEqual(get_summaries(identity_id, limit=1), [])

    def test_batch_review_saves_person_scoped_facts(self):
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "qq:123",
                kind="qq",
                display_name="Alice",
                qq_id="123",
            )
            conn.commit()
        finally:
            conn.close()

        for i in range(5):
            save_message_with_speaker(
                "user",
                f"alice-{i}",
                self.session_id,
                source=CHAT,
                speaker_key="qq:123",
                speaker_name="Alice",
                speaker_qq="123",
            )
            save_message_with_speaker(
                "assistant",
                f"assistant-{i}",
                self.session_id,
                source=CHAT,
                speaker_key="instance",
                speaker_name="璐璐",
            )

        raw = """{
          "summary": "Alice和璐璐聊了事实记录。",
          "familiarity_delta": 0,
          "person_facts": [
            {"subject": "Alice", "scope": "person", "key": "喜欢", "value": "草莓", "confidence": 0.9},
            {"subject": "Alice", "object": "璐璐", "scope": "relationship", "key": "称呼", "value": "Alice会叫璐璐姐姐", "confidence": 0.8}
          ],
          "event_updates": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review
        from pupu.memory import get_person_facts

        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch("pupu.agent.json_task", return_value=raw):
                _maybe_batch_review(self.session_id, identity_session="private_123")

        facts = get_person_facts(subject_person_keys=["qq:123"], include_relationships=True)
        values = {
            (row["subject_person_key"], row["object_person_key"], row["scope"], row["fact_key"]): row["fact_value"]
            for row in facts
        }
        self.assertEqual(values[("qq:123", "", "person", "喜欢")], "草莓")
        self.assertEqual(values[("qq:123", "instance", "relationship", "称呼")], "Alice会叫璐璐姐姐")

    def test_batch_review_updates_existing_candidate_fact(self):
        person_key = "qq:900123"
        identity_session = "private_900123"
        qq_id = "900123"
        display_name = "AliceUpdate"
        conn = get_conn()
        try:
            upsert_person(
                conn,
                person_key,
                kind="qq",
                display_name=display_name,
                qq_id=qq_id,
            )
            conn.commit()
        finally:
            conn.close()

        from pupu.memory import get_person_facts, upsert_person_facts

        upsert_person_facts(
            [{"subject_person_key": person_key, "scope": "person", "key": "外貌", "value": "AliceUpdate没有头发"}],
            known_people=[{"person_key": person_key, "display_name": display_name}],
            legacy_session_id=self.session_id,
        )
        existing = get_person_facts(subject_person_keys=[person_key], include_relationships=False)
        fact_id = existing[0]["id"]

        for i in range(5):
            save_message_with_speaker(
                "user",
                f"AliceUpdate说自己没有刘海 {i}",
                self.session_id,
                source=CHAT,
                speaker_key=person_key,
                speaker_name=display_name,
                speaker_qq=qq_id,
            )
            save_message_with_speaker(
                "assistant",
                f"记住了 {i}",
                self.session_id,
                source=CHAT,
                speaker_key="instance",
                speaker_name="璐璐",
            )

        raw = f"""{{
          "summary": "AliceUpdate补充了外貌事实。",
          "familiarity_delta": 0,
          "fact_updates": [
            {{"action": "update_existing", "fact_id": {fact_id}, "value": "AliceUpdate没有头发，也没有刘海", "confidence": 0.9}}
          ],
          "event_updates": [],
          "task_updates": []
        }}"""

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch("pupu.agent.json_task", return_value=raw) as mock_json_task:
                _maybe_batch_review(self.session_id, identity_session=identity_session)

        prompt_input = mock_json_task.call_args.kwargs["user_content"]
        self.assertIn(f"[fact_id={fact_id}]", prompt_input)
        facts = get_person_facts(subject_person_keys=[person_key], include_relationships=False)
        by_id = {row["id"]: row for row in facts}
        self.assertEqual(by_id[fact_id]["fact_value"], "AliceUpdate没有头发，也没有刘海")

    def test_batch_review_rejects_non_candidate_fact_update(self):
        person_key = "qq:900124"
        identity_session = "private_900124"
        qq_id = "900124"
        display_name = "AliceReject"
        conn = get_conn()
        try:
            upsert_person(
                conn,
                person_key,
                kind="qq",
                display_name=display_name,
                qq_id=qq_id,
            )
            conn.commit()
        finally:
            conn.close()

        from pupu.memory import get_person_facts, upsert_person_facts

        upsert_person_facts(
            [{"subject_person_key": person_key, "scope": "person", "key": "外貌", "value": "AliceReject没有头发"}],
            known_people=[{"person_key": person_key, "display_name": display_name}],
            legacy_session_id=self.session_id,
        )
        existing = get_person_facts(subject_person_keys=[person_key], include_relationships=False)
        fact_id = existing[0]["id"]

        for i in range(5):
            save_message_with_speaker(
                "user",
                f"普通聊天 {i}",
                self.session_id,
                source=CHAT,
                speaker_key=person_key,
                speaker_name=display_name,
                speaker_qq=qq_id,
            )
            save_message_with_speaker(
                "assistant",
                f"普通回复 {i}",
                self.session_id,
                source=CHAT,
                speaker_key="instance",
                speaker_name="璐璐",
            )

        raw = f"""{{
          "summary": "普通聊天。",
          "familiarity_delta": 0,
          "fact_updates": [
            {{"action": "update_existing", "fact_id": {fact_id + 999999}, "value": "不应被写入", "confidence": 0.9}}
          ],
          "event_updates": [],
          "task_updates": []
        }}"""

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.get_pupu_name", return_value="璐璐"):
            with patch("pupu.agent.json_task", return_value=raw):
                _maybe_batch_review(self.session_id, identity_session=identity_session)

        facts = get_person_facts(subject_person_keys=[person_key], include_relationships=False)
        by_id = {row["id"]: row for row in facts}
        self.assertEqual(by_id[fact_id]["fact_value"], "AliceReject没有头发")

    def test_batch_review_omits_familiarity_delta_after_identity_score_reaches_limit(self):
        set_familiarity(100, session_id=self.session_id)
        for i in range(10):
            self._save_chat_turn(i)

        raw = """{
          "summary": "满好感后只整理记忆。",
          "familiarity_delta": 7,
          "event_updates": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value=raw) as mock_json_task:
            _maybe_batch_review(self.session_id)

        system_prompt = mock_json_task.call_args.kwargs["system"]
        summaries = get_summaries(self.session_id, limit=3)

        mock_json_task.assert_called_once()
        self.assertNotIn("familiarity_delta", system_prompt)
        self.assertIn("关系分数已经达到这个身份允许的上限", system_prompt)
        self.assertEqual(get_familiarity(self.session_id), 60)
        self.assertEqual(summaries[-1]["summary"], "满好感后只整理记忆。")

    def test_non_owner_familiarity_is_capped_at_friend_level(self):
        set_familiarity(100, session_id="private_777")
        self.assertEqual(get_familiarity("private_777"), 60)

        update_familiarity(20, session_id="private_777")
        self.assertEqual(get_familiarity("private_777"), 60)

    def test_owner_familiarity_can_reach_lover_level(self):
        set_familiarity(100, session_id="owner")
        self.assertEqual(get_familiarity("owner"), 100)

    def test_batch_review_skips_below_interval_even_after_time_passes(self):
        for i in range(3):
            self._save_chat_turn(i)
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE messages SET timestamp = ? WHERE session_id = ? AND source = 'chat'",
                ("2026-04-26T10:00:00", self.session_id),
            )
            conn.commit()
        finally:
            conn.close()

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value="{}") as mock_json_task:
            _maybe_batch_review(self.session_id)

        mock_json_task.assert_not_called()
        self.assertEqual(get_summaries(self.session_id, limit=3), [])

    def test_single_message_counts_but_waits_for_interval(self):
        save_message("user", "lonely user message", self.session_id, source=CHAT)

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value="{}") as mock_json_task:
            _maybe_batch_review(self.session_id)

        mock_json_task.assert_not_called()
        progress = get_summary_trigger_progress(self.session_id, review_interval=10)
        self.assertEqual(progress["pending"], 1)
        self.assertEqual(progress["remaining"], 9)

    def test_pending_review_sessions_lists_sessions_with_any_chat_message(self):
        self._save_chat_turn(1)
        other_session = self.session_id + "_other"
        reset_session(other_session)
        save_message("user", "single", other_session, source=CHAT)

        self.assertIn(self.session_id, list_pending_review_sessions(source=CHAT))
        self.assertIn(other_session, list_pending_review_sessions(source=CHAT))

    def test_run_due_batch_reviews_scans_pending_sessions(self):
        self._save_chat_turn(1)

        from pupu.agent import run_due_batch_reviews

        with patch("pupu.agent._maybe_batch_review") as mock_review:
            run_due_batch_reviews()

        mock_review.assert_any_call(self.session_id)


if __name__ == "__main__":
    unittest.main()
