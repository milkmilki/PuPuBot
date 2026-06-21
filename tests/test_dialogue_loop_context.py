import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pupu import dialogue_loop
from pupu.instance_context import (
    InstanceContext,
    activate_instance_context,
    get_current_instance_context,
)


class DialogueLoopContextTests(unittest.TestCase):
    def test_wait_followup_uses_sender_instance_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "instances" / "ctx-a"
            (inst / "data" / "logs").mkdir(parents=True)
            (inst / "instance.json").write_text(
                json.dumps({"display_name": "A", "qq_mode": "cli"}),
                encoding="utf-8",
            )
            ctx = InstanceContext.from_instance_dir(inst)
            seen = []
            calls = []

            def fake_chat(*args, **kwargs):
                seen.append(get_current_instance_context())
                calls.append((args, kwargs))
                return "followup"

            delivered = []
            with activate_instance_context(ctx):
                dialogue_loop.register_sender("owner", delivered.append)
            with patch("pupu.agent.chat", side_effect=fake_chat):
                dialogue_loop._on_timer_fire("owner")

            self.assertEqual(seen, [ctx])
            self.assertEqual(delivered, ["followup"])
            self.assertIn("[系统触发的追问]", calls[0][0][0])
            self.assertNotIn("????", calls[0][0][0])
            self.assertNotIn("????", calls[0][1]["reply_speed_hint"])
            self.assertEqual(calls[0][1]["message_source"], "wait_followup")
            dialogue_loop.unregister_sender("owner")


if __name__ == "__main__":
    unittest.main()
