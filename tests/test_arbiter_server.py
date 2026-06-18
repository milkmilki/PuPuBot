import asyncio
import unittest
from unittest.mock import patch

from pupu_console.arbiter_server import _DebounceWatchdog


class ArbiterServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_debounce_watchdog_waits_for_quiet_window(self):
        calls = []
        watchdog = _DebounceWatchdog()

        def fake_run_judge(group_id, *, source):
            calls.append((group_id, source))
            return {"decision_id": len(calls)}

        with patch("pupu_console.arbiter_server._debounce_idle_seconds", return_value=0.05):
            with patch("pupu_console.arbitrator.run_judge", side_effect=fake_run_judge):
                await watchdog.schedule("group-1")
                await asyncio.sleep(0.03)
                await watchdog.schedule("group-1")
                await asyncio.sleep(0.03)
                self.assertEqual(calls, [])
                await asyncio.sleep(0.04)

        self.assertEqual(calls, [("group-1", "debounce")])


if __name__ == "__main__":
    unittest.main()
