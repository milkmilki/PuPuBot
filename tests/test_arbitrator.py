import os
import tempfile
import unittest
from unittest.mock import patch

from pupu_console import arbitrator


class ArbitratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PUPU_REPO_ROOT"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PUPU_REPO_ROOT", None)

    def _payload(self, bot_id="bot_1", round_id="r1"):
        return {
            "group_id": "100",
            "round_id": round_id,
            "my_bot_id": bot_id,
            "my_qq": "111" if bot_id == "bot_1" else "222",
            "my_name": bot_id,
            "my_persona_brief": bot_id,
            "peer": {
                "bot_id": "bot_2" if bot_id == "bot_1" else "bot_1",
                "qq": "222" if bot_id == "bot_1" else "111",
                "name": "peer",
                "persona_brief": "peer",
            },
            "recent_context": "User: hello",
            "min_bot_gap_seconds": 0,
        }

    def test_explicit_at_selects_target_without_llm(self):
        payload = self._payload()
        payload["at_targets"] = ["222"]

        with patch("pupu_console.arbitrator._llm_decide") as mock_llm:
            decision = arbitrator.arbitrate(payload)

        self.assertEqual(decision["speaker"], "bot_2")
        self.assertEqual(decision["reason"], "explicit_at")
        mock_llm.assert_not_called()

    def test_decision_is_cached_for_same_round(self):
        with patch.object(arbitrator, "_DEFAULT_WAIT_SECONDS", 0.01):
            with patch(
                "pupu_console.arbitrator._effective_merge_round",
                return_value="merge:test",
            ):
                with patch(
                    "pupu_console.arbitrator._llm_decide",
                    return_value=("bot_1", "test", 0.9),
                ) as mock_llm:
                    first = arbitrator.arbitrate(self._payload(round_id="cached"))
                    second = arbitrator.arbitrate(self._payload(bot_id="bot_2", round_id="different"))

        self.assertEqual(first["speaker"], "bot_1")
        self.assertEqual(second["speaker"], "bot_1")
        mock_llm.assert_called_once()

    def test_llm_failure_degrades_to_none(self):
        with patch.object(arbitrator, "_DEFAULT_WAIT_SECONDS", 0.01):
            with patch(
                "pupu_console.arbitrator._llm_decide",
                return_value=("none", "llm_failed:boom", 0.0),
            ):
                decision = arbitrator.arbitrate(self._payload(round_id="fail"))

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


if __name__ == "__main__":
    unittest.main()
