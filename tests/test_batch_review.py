import os
from pathlib import Path
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.agent import _parse_batch_review_result
from pupu.memory import (
    _get_conn,
    create_scheduled_task,
    get_familiarity,
    get_important_events,
    get_pending_review_last_message_time,
    get_review_candidate_batch,
    get_summaries,
    get_summary_trigger_progress,
    init_db,
    list_pending_review_sessions,
    list_scheduled_tasks,
    reset_session,
    save_message,
    save_summary,
    set_familiarity,
    update_familiarity,
)

from pupu.message_sources import CHAT, PROACTIVE, SCHEDULED


class BatchReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_batch_review_{self._testMethodName}"
        reset_session(self.session_id)

    def _save_chat_turn(self, index: int):
        save_message("user", f"user-{index}", self.session_id, source=CHAT)
        save_message("assistant", f"assistant-{index}", self.session_id, source=CHAT)

    def _set_chat_timestamps(self, moment: datetime):
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE messages SET timestamp = ? WHERE session_id = ? AND source = 'chat'",
                (moment.isoformat(timespec="seconds"), self.session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_summary_progress_counts_completed_chat_turns(self):
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

        self.assertEqual(progress["pending"], 3)
        self.assertEqual(progress["remaining"], 5)
        self.assertFalse(progress["ready"])

    def test_review_candidate_batch_uses_full_turns_and_skips_internal_sources(self):
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

        self.assertEqual(sum(1 for item in batch if item["role"] == "assistant"), 8)
        self.assertTrue(batch)
        self.assertTrue(all(item["source"] == CHAT for item in batch))
        self.assertEqual(batch[0]["content"], "user-0")
        self.assertEqual(batch[-1]["content"], "assistant-7")

    def test_saved_summary_advances_review_cursor_by_batch_end(self):
        for i in range(10):
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

        self.assertEqual(progress["pending"], 2)
        self.assertEqual(progress["remaining"], 6)
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
  "user_facts": {"favorite_genre": "fantasy",},
  "self_facts": {},
  "important_events": [{
    "source_event_key": "birthday-2026-04-27",
    "title": "user birthday tomorrow",
    "kind": "birthday",
    "event_time": "2026-04-27",
    "time_text": "tomorrow",
    "details": "user said birthday tomorrow",
    "followup_hint": "wish happy birthday",
    "confidence": 0.9
  },],
  "task_updates": [{
    "action": "create",
    "source_event_key": "birthday-2026-04-27",
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
        self.assertEqual(parsed["user_facts"]["favorite_genre"], "fantasy")
        self.assertEqual(
            parsed["important_events"][0]["source_event_key"],
            "birthday-2026-04-27",
        )
        self.assertEqual(parsed["task_updates"][0]["action"], "create")

    def test_parse_batch_review_result_repairs_unescaped_quotes_inside_strings(self):
        raw = """```json
{
  "summary": "用户用"永远在一起"表达想做一辈子朋友。",
  "familiarity_delta": 8,
  "user_facts": {
    "commitment": "用户承诺要做仆仆一辈子的朋友，永远在一起"
  },
  "self_facts": {},
  "important_events": [
    {
      "source_event_key": "eternal_friendship_promise",
      "title": "永远的朋友承诺",
      "kind": "promise",
      "event_time": "2026-04-26",
      "time_text": "今天",
      "details": "用户和仆仆确立了永远做朋友的承诺。",
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
            parsed["important_events"][0]["source_event_key"],
            "eternal_friendship_promise",
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
        for i in range(8):
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
          "user_facts": {},
          "self_facts": {},
          "important_events": [{
            "source_event_key": "promise-test",
            "title": "测试约定",
            "kind": "promise",
            "event_time": "",
            "time_text": "刚才",
            "details": "用户和仆仆说好要记住这件事。",
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
        events = get_important_events(self.session_id, limit=3)
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
        self.assertEqual(events[0]["source_event_key"], "promise-test")
        self.assertEqual(list_scheduled_tasks(self.session_id), [])

    def test_batch_review_splits_context_summary_and_identity_memory(self):
        context_id = self.session_id + "_context"
        identity_id = self.session_id + "_identity"
        reset_session(context_id)
        reset_session(identity_id)
        set_familiarity(0, session_id=identity_id)
        for i in range(8):
            save_message("user", f"user-{i}", context_id, source=CHAT)
            save_message("assistant", f"assistant-{i}", context_id, source=CHAT)

        raw = """{
          "summary": "群上下文摘要。",
          "familiarity_delta": 2,
          "user_facts": {"喜欢": "草莓"},
          "self_facts": {},
          "important_events": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review
        from pupu.memory import get_user_facts

        with patch("pupu.agent.json_task", return_value=raw):
            _maybe_batch_review(context_id, identity_session=identity_id)

        self.assertEqual(get_summaries(context_id, limit=1)[-1]["summary"], "群上下文摘要。")
        self.assertEqual(get_familiarity(identity_id), 2)
        self.assertEqual(get_user_facts(identity_id), {"喜欢": "草莓"})
        self.assertEqual(get_summaries(identity_id, limit=1), [])

    def test_batch_review_omits_familiarity_delta_after_score_reaches_100(self):
        set_familiarity(100, session_id=self.session_id)
        for i in range(8):
            self._save_chat_turn(i)

        raw = """{
          "summary": "满好感后只整理记忆。",
          "familiarity_delta": 7,
          "user_facts": {},
          "self_facts": {},
          "important_events": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value=raw) as mock_json_task:
            _maybe_batch_review(self.session_id)

        system_prompt = mock_json_task.call_args.kwargs["system"]
        summaries = get_summaries(self.session_id, limit=3)

        mock_json_task.assert_called_once()
        self.assertNotIn("familiarity_delta", system_prompt)
        self.assertIn("关系分数已经达到 100", system_prompt)
        self.assertEqual(get_familiarity(self.session_id), 100)
        self.assertEqual(summaries[-1]["summary"], "满好感后只整理记忆。")

    def test_idle_batch_review_triggers_below_interval(self):
        for i in range(3):
            self._save_chat_turn(i)
        self._set_chat_timestamps(datetime.now() - timedelta(seconds=700))

        raw = """{
          "summary": "短对话停下来后也被总结。",
          "familiarity_delta": 0,
          "user_facts": {},
          "self_facts": {},
          "important_events": [],
          "task_updates": []
        }"""

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value=raw) as mock_json_task:
            _maybe_batch_review(self.session_id)

        summaries = get_summaries(self.session_id, limit=3)
        mock_json_task.assert_called_once()
        self.assertEqual(summaries[-1]["summary"], "短对话停下来后也被总结。")

    def test_idle_batch_review_skips_recent_pending(self):
        for i in range(3):
            self._save_chat_turn(i)
        self._set_chat_timestamps(datetime.now())

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value="{}") as mock_json_task:
            _maybe_batch_review(self.session_id)

        mock_json_task.assert_not_called()
        self.assertEqual(get_summaries(self.session_id, limit=3), [])

    def test_idle_batch_review_ignores_half_turn(self):
        save_message("user", "lonely user message", self.session_id, source=CHAT)
        self._set_chat_timestamps(datetime.now() - timedelta(seconds=700))

        from pupu.agent import _maybe_batch_review

        with patch("pupu.agent.json_task", return_value="{}") as mock_json_task:
            _maybe_batch_review(self.session_id)

        mock_json_task.assert_not_called()

    def test_pending_review_sessions_lists_sessions_with_complete_turns(self):
        self._save_chat_turn(1)
        other_session = self.session_id + "_other"
        reset_session(other_session)
        save_message("user", "half", other_session, source=CHAT)

        self.assertIn(self.session_id, list_pending_review_sessions(source=CHAT))
        self.assertNotIn(other_session, list_pending_review_sessions(source=CHAT))

    def test_run_due_batch_reviews_scans_pending_sessions(self):
        self._save_chat_turn(1)

        from pupu.agent import run_due_batch_reviews

        with patch("pupu.agent._maybe_batch_review") as mock_review:
            run_due_batch_reviews()

        mock_review.assert_any_call(self.session_id)


if __name__ == "__main__":
    unittest.main()
