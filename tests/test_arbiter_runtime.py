import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pupu.arbiter_runtime import EmbeddedArbiterRuntime
from pupu.logging_utils import close_all_log_sinks


class EmbeddedArbiterRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in ("PUPU_REPO_ROOT", "PUPU_ARBITER_AUDIT")
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmp.name
        os.environ["PUPU_ARBITER_AUDIT"] = "0"
        (Path(self._tmp.name) / "instances" / "_shared").mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self) -> None:
        close_all_log_sinks()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def test_observe_waits_for_quiet_window_before_judging(self) -> None:
        runtime = EmbeddedArbiterRuntime()
        calls = []

        def fake_run_judge(group_id, *, source):
            calls.append((group_id, source))
            return {"decision_id": len(calls), "group_id": group_id, "speaker": "bot-a"}

        payload = {
            "group_id": "900",
            "message_id": "msg-1",
            "speaker_qq": "111",
            "speaker_name": "A",
            "text": "hi",
            "reporter": {"bot_id": "bot-a", "name": "A"},
        }
        with patch("pupu_console.arbitrator.run_judge", side_effect=fake_run_judge):
            await runtime.observe(payload, debounce_seconds=0.05)
            await asyncio.sleep(0.03)
            await runtime.observe({**payload, "message_id": "msg-2"}, debounce_seconds=0.05)
            await asyncio.sleep(0.03)
            self.assertEqual(calls, [])
            await asyncio.sleep(0.12)

        self.assertEqual(calls, [("900", "embedded")])
        await runtime.close()

    async def test_silence_is_persisted_without_http_service(self) -> None:
        runtime = EmbeddedArbiterRuntime()
        self.assertFalse(runtime.is_silenced("900"))
        result = runtime.set_silence("900", True)
        self.assertTrue(result["ok"])
        self.assertTrue(runtime.is_silenced("900"))
        runtime.set_silence("900", False)
        self.assertFalse(runtime.is_silenced("900"))
        await runtime.close()


if __name__ == "__main__":
    unittest.main()
