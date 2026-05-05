import unittest
from unittest.mock import patch


class OpenGroupHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import nonebot

        try:
            nonebot.get_driver()
        except ValueError:
            nonebot.init(driver="~fastapi")
        from plugins.pupu_support import onebot_handlers

        cls.onebot_handlers = onebot_handlers

    def test_speaker_prefix_marks_peer_bot(self):
        if not hasattr(self.onebot_handlers, "_speaker_prefix"):
            self.skipTest("onebot adapter not installed")

        with patch(
            "plugins.pupu_support.onebot_handlers.load_peer_config",
            return_value={"qq": "222", "name": "小白"},
        ):
            prefix, is_bot = self.onebot_handlers._speaker_prefix("222", "raw")

        self.assertTrue(is_bot)
        self.assertEqual(prefix, "[bot 小白(QQ:222)] ")

    def test_speaker_prefix_marks_human(self):
        if not hasattr(self.onebot_handlers, "_speaker_prefix"):
            self.skipTest("onebot adapter not installed")

        with patch(
            "plugins.pupu_support.onebot_handlers.load_peer_config",
            return_value={"qq": "222", "name": "小白"},
        ):
            prefix, is_bot = self.onebot_handlers._speaker_prefix("111", "用户")

        self.assertFalse(is_bot)
        self.assertEqual(prefix, "[用户(QQ:111)] ")


if __name__ == "__main__":
    unittest.main()
