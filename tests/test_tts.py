import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plugins.pupu_support import state
from plugins.pupu_support.common import send_segments
from pupu.tts import TTSConfig, synthesize_reply_to_file


async def _no_sleep(_seconds):
    return None


class FakeResponse:
    def __init__(self, status_code=200, content=b"RIFFaudio", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send(self, event, message):
        self.messages.append(message)


class TTSTests(unittest.TestCase):
    def _config(self, tmp: Path, *, enabled=True, max_chars=120):
        ref_audio = tmp / "pupu_ref.wav"
        ref_audio.write_bytes(b"fake")
        return TTSConfig(
            enabled=enabled,
            base_url="http://127.0.0.1:9880",
            ref_audio=str(ref_audio),
            prompt_text="これは参照音声です",
            prompt_lang="ja",
            text_lang="zh",
            max_chars=max_chars,
            timeout=3,
            media_type="wav",
            cache_dir=tmp / "cache",
            text_split_method="cut5",
            top_k=5,
            top_p=0.85,
            temperature=0.65,
            repetition_penalty=1.15,
            parallel_infer=False,
        )

    def test_synthesize_writes_audio_file(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            config = self._config(tmp)

            with patch("pupu.tts.httpx.post", return_value=FakeResponse()) as mock_post:
                path = synthesize_reply_to_file("你好\n姐姐在", config)

            self.assertIsNotNone(path)
            self.assertEqual(path.read_bytes(), b"RIFFaudio")
            payload = mock_post.call_args.kwargs["json"]
            self.assertEqual(payload["text"], "你好。姐姐在。")
            self.assertEqual(payload["prompt_lang"], "ja")
            self.assertEqual(payload["text_lang"], "zh")
            self.assertEqual(payload["top_k"], 5)
            self.assertEqual(payload["top_p"], 0.85)
            self.assertEqual(payload["temperature"], 0.65)
            self.assertEqual(payload["repetition_penalty"], 1.15)
            self.assertFalse(payload["parallel_infer"])

    def test_synthesize_skips_when_disabled_or_too_long(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            disabled = self._config(tmp, enabled=False)
            too_long = self._config(tmp, enabled=True, max_chars=2)

            with patch("pupu.tts.httpx.post") as mock_post:
                self.assertIsNone(synthesize_reply_to_file("你好", disabled))
                self.assertIsNone(synthesize_reply_to_file("你好呀", too_long))

            mock_post.assert_not_called()

    def test_synthesize_returns_none_when_service_fails(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            config = self._config(tmp)

            with patch("pupu.tts.httpx.post", side_effect=RuntimeError("offline")):
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
                await send_segments(bot, object(), ["鍙彂鏂囧瓧"])

        self.assertEqual(bot.messages, ["鍙彂鏂囧瓧"])
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
