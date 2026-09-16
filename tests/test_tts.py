from pathlib import Path
import tempfile
import unittest

from pupu.tts import (
    TTSConfig,
    get_tts_status,
    register_tts_provider,
    synthesize_reply_to_file,
    unregister_tts_provider,
)


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
                self.assertEqual(text, "hello。sister is here。")
                self.assertEqual(cfg.voice, "pupu")
                return b"RIFFaudio", "wav"

            register_tts_provider("mock", provider)
            path = synthesize_reply_to_file("hello\nsister is here", config)

            self.assertIsNotNone(path)
            self.assertEqual(path.read_bytes(), b"RIFFaudio")

    def test_synthesize_skips_when_disabled_too_long_or_provider_missing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            disabled = self._config(tmp, enabled=False)
            too_long = self._config(tmp, enabled=True, max_chars=2)
            missing = self._config(tmp, enabled=True, provider="")
            unavailable = self._config(tmp, enabled=True, provider="not_installed")

            self.assertIsNone(synthesize_reply_to_file("hi", disabled))
            self.assertIsNone(synthesize_reply_to_file("hello", too_long))
            self.assertIsNone(synthesize_reply_to_file("hi", missing))
            self.assertIsNone(synthesize_reply_to_file("hi", unavailable))

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
            self.assertIsNone(synthesize_reply_to_file("hi", config))


if __name__ == "__main__":
    unittest.main()
