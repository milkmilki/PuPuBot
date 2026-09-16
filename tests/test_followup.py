import unittest

from pupu.followup_manager import cancel_timer, create_timer, drain_fired, has_timer
from pupu import followup
from pupu.sessions import OWNER_SESSION


class FollowupModuleTests(unittest.TestCase):
    def test_parse_dialogue_output_json(self):
        content, should_wait = followup._parse_dialogue_output(
            '{"content":"你想吃什么？","should_wait":true}'
        )
        self.assertEqual(content, "你想吃什么？")
        self.assertTrue(should_wait)

    def test_parse_dialogue_output_fallback(self):
        content, should_wait = followup._parse_dialogue_output("要不要现在回我？")
        self.assertEqual(content, "要不要现在回我？")
        self.assertTrue(should_wait)

    def test_followup_timer_manager_roundtrip(self):
        session_id = OWNER_SESSION
        self.assertFalse(has_timer(session_id))
        create_timer(session_id, 0.01, lambda: None)
        self.assertTrue(has_timer(session_id))
        self.assertTrue(cancel_timer(session_id))
        self.assertFalse(has_timer(session_id))

    def test_followup_timer_manager_drain_fired(self):
        session_id = OWNER_SESSION
        create_timer(session_id, 0.01, lambda: None)
        import time

        time.sleep(0.05)
        fired = drain_fired(4)
        self.assertIn(session_id, fired)
        cancel_timer(session_id)


if __name__ == "__main__":
    unittest.main()
