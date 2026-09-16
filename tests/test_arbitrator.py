import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from pupu_console import arbitrator


class ArbitratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PUPU_REPO_ROOT"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PUPU_REPO_ROOT", None)

    def _observe_message(self, *, text="User: hello", message_id="msg-1", speaker_qq="333"):
        return arbitrator.observe(
            {
                "group_id": "100",
                "message_id": message_id,
                "speaker_qq": speaker_qq,
                "speaker_name": "user",
                "speaker_is_bot": False,
                "text": text,
                "reporter": {"bot_id": "bot_1", "qq": "111001", "name": "bot_1"},
                "peers": [{"bot_id": "bot_2", "qq": "222002", "name": "bot_2"}],
            }
        )

    def test_explicit_at_selects_target_without_llm(self):
        self._observe_message(text="@222002 来一下")
        with patch("pupu_console.arbitrator._llm_decide") as mock_llm:
            decision = arbitrator.run_judge("100")

        self.assertIsNotNone(decision)
        self.assertEqual(decision["speaker"], "bot_2")
        self.assertEqual(decision["reason"], "explicit_at")
        mock_llm.assert_not_called()

    def test_decision_can_be_loaded_by_waiters(self):
        self._observe_message()
        with patch(
            "pupu_console.arbitrator._llm_decide",
            return_value=("bot_1", "test", 0.9),
        ) as mock_llm:
            decision = arbitrator.run_judge("100")

        self.assertIsNotNone(decision)
        self.assertEqual(decision["speaker"], "bot_1")
        loaded = arbitrator.load_decision_after("100", int(decision["decision_id"]) - 1)
        self.assertEqual(loaded, decision)
        mock_llm.assert_called_once()

    def test_expired_decision_is_not_loaded_by_waiters(self):
        self._observe_message()
        conn = arbitrator._connect()
        try:
            now = arbitrator._now()
            cursor = conn.execute(
                """
                INSERT INTO group_decisions
                    (group_id, speaker, reason, confidence, since_message_id, decided_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "100",
                    "bot_1",
                    "old",
                    1.0,
                    "msg-1",
                    arbitrator._iso(now - timedelta(minutes=10)),
                    arbitrator._iso(now - timedelta(minutes=5)),
                ),
            )
            decision_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()

        self.assertIsNone(arbitrator.load_decision_after("100", decision_id - 1))

    def test_llm_failure_degrades_to_none(self):
        self._observe_message()
        with patch(
            "pupu_console.arbitrator._llm_decide",
            return_value=("none", "llm_failed:boom", 0.0),
        ):
            decision = arbitrator.run_judge("100")

        self.assertIsNotNone(decision)
        self.assertEqual(decision["speaker"], "none")

    def test_recent_context_uses_canonical_speaker_names(self):
        messages = [
            {
                "message_id": "1",
                "speaker_qq": "424225912",
                "speaker_person_key": "owner",
                "speaker_name": "小夫",
                "speaker_is_bot": False,
                "text": "大家都是我老婆",
            },
            {
                "message_id": "2",
                "speaker_qq": "3853876778",
                "speaker_person_key": "qq:3853876778",
                "speaker_name": "仆仆",
                "speaker_is_bot": True,
                "text": "你想得挺美",
            },
        ]

        context, _targets, since = arbitrator._build_recent_context(messages)

        self.assertEqual(since, "2")
        self.assertIn("[小夫] 大家都是我老婆", context)
        self.assertIn("[bot 仆仆] 你想得挺美", context)
        self.assertNotIn("424225912", context)
        self.assertNotIn("钮钴禄", context)


    def test_observe_replaces_stale_bot_id_for_same_qq(self):
        old = {
            "group_id": "100",
            "message_id": "old",
            "text": "old",
            "reporter": {"bot_id": "old-instance", "qq": "111", "name": "old"},
        }
        new = {
            "group_id": "100",
            "message_id": "new",
            "text": "new",
            "reporter": {"bot_id": "111", "qq": "111", "name": "new"},
        }

        self.assertTrue(arbitrator.observe(old)["ok"])
        self.assertTrue(arbitrator.observe(new)["ok"])

        conn = arbitrator._connect()
        try:
            rows = conn.execute(
                "SELECT bot_id FROM group_bots WHERE group_id = ? AND qq = ? ORDER BY bot_id",
                ("100", "111"),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual([row["bot_id"] for row in rows], ["111"])


if __name__ == "__main__":
    unittest.main()
