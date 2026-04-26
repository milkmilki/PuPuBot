import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.agent import _parse_batch_review_result
from pupu.memory import (
    get_review_candidate_batch,
    get_summary_trigger_progress,
    init_db,
    reset_session,
    save_message,
    save_summary,
)


class BatchReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = "test_batch_review"
        reset_session(self.session_id)

    def _save_chat_turn(self, index: int):
        save_message("user", f"user-{index}", self.session_id, source="chat")
        save_message("assistant", f"assistant-{index}", self.session_id, source="chat")

    def test_summary_progress_counts_completed_chat_turns(self):
        for i in range(3):
            self._save_chat_turn(i)

        save_message("assistant", "proactive ping", self.session_id, source="proactive")
        save_message("user", "scheduled user", self.session_id, source="scheduled")
        save_message(
            "assistant",
            "scheduled assistant",
            self.session_id,
            source="scheduled",
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
                    source="proactive",
                )

        batch = get_review_candidate_batch(
            session_id=self.session_id,
            review_interval=8,
            source="chat",
        )

        self.assertEqual(sum(1 for item in batch if item["role"] == "assistant"), 8)
        self.assertTrue(batch)
        self.assertTrue(all(item["source"] == "chat" for item in batch))
        self.assertEqual(batch[0]["content"], "user-0")
        self.assertEqual(batch[-1]["content"], "assistant-7")

    def test_saved_summary_advances_review_cursor_by_batch_end(self):
        for i in range(10):
            self._save_chat_turn(i)

        batch = get_review_candidate_batch(
            session_id=self.session_id,
            review_interval=8,
            source="chat",
        )
        save_summary("batch one", batch[0]["id"], batch[-1]["id"], self.session_id)

        progress = get_summary_trigger_progress(self.session_id, review_interval=8)
        next_batch = get_review_candidate_batch(
            session_id=self.session_id,
            review_interval=8,
            source="chat",
        )

        self.assertEqual(progress["pending"], 2)
        self.assertEqual(progress["remaining"], 6)
        self.assertEqual(next_batch, [])

    def test_parse_batch_review_result_handles_fences_and_trailing_commas(self):
        raw = """```json
{
  "summary": "聊了电影",
  "familiarity_events": [{"delta": 2, "reason": "气氛不错"},],
  "user_facts": {"喜欢的类型": "奇幻",},
  "self_facts": {}
}
```"""

        parsed = _parse_batch_review_result(raw)

        self.assertEqual(parsed["summary"], "聊了电影")
        self.assertEqual(parsed["familiarity_events"][0]["delta"], 2)
        self.assertEqual(parsed["user_facts"]["喜欢的类型"], "奇幻")


if __name__ == "__main__":
    unittest.main()
