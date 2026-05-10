import unittest

from pupu.stardew_bridge import (
    StardewBridgeConfig,
    format_stardew_user_input,
    handle_chat_payload,
)


class StardewBridgeTests(unittest.TestCase):
    def test_format_stardew_user_input_marks_npc_context(self):
        text = format_stardew_user_input(
            "仆仆今天种什么",
            {
                "npc_name": "仆仆",
                "player": "大宁",
                "farm": "小狗农场",
                "location": "Farm",
                "season": "spring",
                "day": 3,
                "time": 930,
            },
        )

        self.assertIn("[星露谷NPC", text)
        self.assertIn("对话对象=仆仆", text)
        self.assertIn("玩家=大宁", text)
        self.assertIn("地点=Farm", text)
        self.assertTrue(text.endswith("仆仆今天种什么"))

    def test_handle_chat_payload_uses_owner_memory_and_chat_source(self):
        calls = []

        def fake_chat(user_input, session_id, **kwargs):
            calls.append((user_input, session_id, kwargs))
            return "种草莓吧"

        result = handle_chat_payload(
            {
                "text": "今天种什么",
                "context": {"npc_name": "仆仆", "location": "Farm"},
            },
            config=StardewBridgeConfig(session_id="owner"),
            chat_func=fake_chat,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "owner")
        self.assertEqual(result["reply"], "种草莓吧")
        self.assertEqual(calls[0][1], "owner")
        self.assertEqual(calls[0][2]["message_source"], "chat")
        self.assertFalse(calls[0][2]["is_admin"])
        self.assertIn("[星露谷NPC", calls[0][0])

    def test_handle_chat_payload_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            handle_chat_payload(
                {"text": " "},
                config=StardewBridgeConfig(session_id="owner"),
                chat_func=lambda *args, **kwargs: "",
            )


if __name__ == "__main__":
    unittest.main()
