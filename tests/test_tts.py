import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plugins.pupu_support import state
from plugins.pupu_support.common import send_segments
from pupu.tts import (
    TTSConfig,
    get_tts_status,
    register_tts_provider,
    synthesize_reply_to_file,
    unregister_tts_provider,
)


async def _no_sleep(_seconds):
    return None


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send(self, event, message):
        self.messages.append(message)


class TTSTests(unittest.TestCase):
    def tearDown(self):
        unregister_tts_provider("mock")

    def _config(self, tmp: Path, *, enabled=True, provider="mock", max_chars=120):
        return TTSConfig(
            enabled=enabled,
            provider=provider,
            base_url="http://127.0.0.1:9880",
            voice="pupu",
            max_chars=max_chars,
            timeout=3,
            audio_format="wav",
            cache_dir=tmp / "cache",
            normalize_audio=False,
            ffmpeg_path="",
        )

    def test_synthesize_writes_audio_file_via_registered_provider(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            config = self._config(tmp)

            def provider(text: str, cfg: TTSConfig):
                self.assertEqual(text, "你好。姐姐在。")
                self.assertEqual(cfg.voice, "pupu")
                return b"RIFFaudio", "wav"

            register_tts_provider("mock", provider)
            path = synthesize_reply_to_file("你好\n姐姐在", config)

            self.assertIsNotNone(path)
            self.assertEqual(path.read_bytes(), b"RIFFaudio")

    def test_synthesize_skips_when_disabled_too_long_or_provider_missing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            disabled = self._config(tmp, enabled=False)
            too_long = self._config(tmp, enabled=True, max_chars=2)
            missing = self._config(tmp, enabled=True, provider="")
            unavailable = self._config(tmp, enabled=True, provider="not_installed")

            self.assertIsNone(synthesize_reply_to_file("你好", disabled))
            self.assertIsNone(synthesize_reply_to_file("你好呀", too_long))
            self.assertIsNone(synthesize_reply_to_file("你好", missing))
            self.assertIsNone(synthesize_reply_to_file("你好", unavailable))

    def test_status_reports_provider_readiness(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            register_tts_provider("mock", lambda text, cfg: None)

            ready = get_tts_status(self._config(tmp))
            self.assertTrue(ready.ready)
            self.assertEqual(ready.reason, "ok")

            missing = get_tts_status(self._config(tmp, provider=""))
            self.assertFalse(missing.ready)
            self.assertEqual(missing.reason, "provider_missing")

            unavailable = get_tts_status(self._config(tmp, provider="ghost"))
            self.assertFalse(unavailable.ready)
            self.assertEqual(unavailable.reason, "provider_unavailable")

    def test_synthesize_returns_none_when_provider_fails(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            config = self._config(tmp)

            def provider(_text: str, _cfg: TTSConfig):
                raise RuntimeError("offline")

            register_tts_provider("mock", provider)
            self.assertIsNone(synthesize_reply_to_file("你好", config))


class TTSOneBotSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._previous_tts_reply_enabled = state.tts_reply_enabled
        state.tts_reply_enabled = False

    async def asyncTearDown(self):
        state.tts_reply_enabled = self._previous_tts_reply_enabled

    async def test_send_segments_appends_voice_after_text(self):
        state.tts_reply_enabled = True
        bot = FakeBot()
        with tempfile.TemporaryDirectory() as raw_tmp:
            audio = Path(raw_tmp) / "voice.wav"
            audio.write_bytes(b"voice")
            with patch("plugins.pupu_support.common._is_onebot_v11_bot", return_value=True):
                with patch("plugins.pupu_support.common.synthesize_reply_to_file", return_value=audio):
                    with patch("plugins.pupu_support.common.asyncio.sleep", _no_sleep):
                        await send_segments(bot, object(), ["第一句", "第二句"])

        self.assertEqual(bot.messages[0], "第一句")
        self.assertEqual(bot.messages[1], "第二句")
        self.assertEqual(getattr(bot.messages[2], "type", ""), "record")

    async def test_send_segments_skips_voice_when_switch_off(self):
        bot = FakeBot()
        with patch("plugins.pupu_support.common._is_onebot_v11_bot", return_value=True):
            with patch("plugins.pupu_support.common.synthesize_reply_to_file") as mock_tts:
                await send_segments(bot, object(), ["只发文字"])

        self.assertEqual(bot.messages, ["只发文字"])
        mock_tts.assert_not_called()

    async def test_send_segments_keeps_text_when_tts_unavailable(self):
        state.tts_reply_enabled = True
        bot = FakeBot()
        with patch("plugins.pupu_support.common._is_onebot_v11_bot", return_value=True):
            with patch("plugins.pupu_support.common.synthesize_reply_to_file", return_value=None):
                await send_segments(bot, object(), ["只发文字"])

        self.assertEqual(bot.messages, ["只发文字"])


if __name__ == "__main__":
    unittest.main()
