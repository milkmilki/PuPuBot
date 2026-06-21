import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pupu.actor import InstanceActor
from pupu.hooks import clear_hooks, list_hooks, register_hook
from pupu.instance_context import (
    InstanceContext,
    activate_instance_context,
    activate_instance_context_global,
    clear_instance_context_global,
    get_current_instance_context,
)
from pupu.logging_utils import close_current_instance_log_sinks
from pupu.memory import init_db, reset_session, save_message
from pupu.message_sources import CHAT


class HookLayerTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        clear_hooks()

    def _make_ctx(self, root: Path, iid: str = "hooked") -> InstanceContext:
        inst = root / "instances" / iid
        (inst / "data" / "logs").mkdir(parents=True)
        (inst / "instance.json").write_text(
            json.dumps(
                {
                    "id": iid,
                    "display_name": "Hook Bot",
                    "qq_mode": "cli",
                    "owner_ids": ["111"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (inst / "persona.json").write_text(
            json.dumps({"name": "Hook Bot"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return InstanceContext.from_instance_dir(inst)

    async def test_register_hook_returns_unregister(self) -> None:
        seen = []

        unregister = register_hook("instance.status", lambda event: seen.append(event.name))
        self.assertEqual(list_hooks(), {"instance.status": 1})
        unregister()
        self.assertEqual(list_hooks(), {})

    async def test_instance_actor_emits_status_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp))
            with activate_instance_context(ctx):
                init_db()
            seen: list[tuple[str, str, str]] = []

            async def on_status(event):
                seen.append(
                    (
                        event.payload["status"],
                        event.payload["instance_id"],
                        event.payload["display_name"],
                    )
                )

            register_hook("instance.status", on_status)
            actor = InstanceActor(
                ctx,
                preflight=False,
                start_background_tasks=False,
            )

            await actor.start()
            await actor.stop()

        self.assertEqual(
            [status for status, _iid, _name in seen],
            ["starting", "running", "stopping", "stopped"],
        )
        self.assertTrue(all(iid == "hooked" for _status, iid, _name in seen))
        self.assertTrue(all(name == "Hook Bot" for _status, _iid, name in seen))

    async def test_hook_error_does_not_break_actor_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "hook-error")
            with activate_instance_context(ctx):
                init_db()

            def bad_hook(_event):
                raise RuntimeError("boom")

            register_hook("instance.status", bad_hook)
            actor = InstanceActor(ctx, preflight=False, start_background_tasks=False)
            await actor.start()
            self.assertTrue(actor.running)
            await actor.stop()

    async def test_actor_start_failure_emits_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "hook-failed")
            seen: list[tuple[str, str]] = []

            register_hook(
                "instance.status",
                lambda event: seen.append(
                    (
                        event.payload["status"],
                        event.payload.get("error", ""),
                    )
                ),
            )
            actor = InstanceActor(ctx, preflight=False, start_background_tasks=False)
            actor.context.config_path.write_text(
                json.dumps(
                    {
                        "id": "hook-failed",
                        "display_name": "Hook Failed",
                        "qq_mode": "bad-mode",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            actor.context = InstanceContext.from_instance_dir(ctx.instance_dir)

            with self.assertRaises(RuntimeError):
                await actor.start()

        self.assertEqual([status for status, _error in seen], ["starting", "failed"])
        self.assertIn("RuntimeError", seen[-1][1])


class ChatAndMemoryHookTests(unittest.TestCase):
    def _make_ctx(self, root: Path, iid: str = "hook-chat") -> InstanceContext:
        inst = root / "instances" / iid
        (inst / "data" / "logs").mkdir(parents=True)
        (inst / "instance.json").write_text(
            json.dumps(
                {
                    "id": iid,
                    "display_name": "Hook Chat Bot",
                    "qq_mode": "cli",
                    "owner_ids": ["111"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (inst / "persona.json").write_text(
            json.dumps({"name": "Hook Chat Bot"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return InstanceContext.from_instance_dir(inst)

    def setUp(self) -> None:
        clear_hooks()
        self._previous_context = get_current_instance_context()
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = self._make_ctx(Path(self._tmp.name), f"hook-chat-{self._testMethodName}")
        activate_instance_context_global(self.ctx)
        init_db()
        self.session_id = f"hook_chat_{self._testMethodName}"
        reset_session(self.session_id)

    def tearDown(self) -> None:
        try:
            reset_session(self.session_id)
        finally:
            clear_hooks()
            close_current_instance_log_sinks()
            if self._previous_context is not None:
                activate_instance_context_global(self._previous_context)
            else:
                clear_instance_context_global()
            self._tmp.cleanup()

    def test_chat_emits_started_and_reply_created(self) -> None:
        from pupu.agent import chat

        seen = []
        register_hook(
            "chat.started",
            lambda event: seen.append((event.name, dict(event.payload))),
        )
        register_hook(
            "chat.reply_created",
            lambda event: seen.append((event.name, dict(event.payload))),
        )

        with patch("pupu.agent.is_memu_long_term_enabled", return_value=False):
            with patch("pupu.agent.chat_complete", return_value='{"content":"好呀","should_wait":false}'):
                with patch("pupu.agent._maybe_batch_review", return_value=None):
                    reply = chat("hello", session_id=self.session_id, is_admin=True)

        self.assertEqual(reply, "好呀")
        self.assertEqual([name for name, _payload in seen], ["chat.started", "chat.reply_created"])
        self.assertEqual(seen[0][1]["context_session"], self.session_id)
        self.assertEqual(seen[0][1]["input_preview"], "hello")
        self.assertEqual(seen[1][1]["reply_preview"], "好呀")
        self.assertFalse(seen[1][1]["should_wait"])

    def test_chat_error_hook_fires_when_model_fails(self) -> None:
        from pupu.agent import chat

        seen = []
        register_hook("chat.error", lambda event: seen.append(dict(event.payload)))

        with patch("pupu.agent.is_memu_long_term_enabled", return_value=False):
            with patch("pupu.agent.chat_complete", side_effect=RuntimeError("model down")):
                with self.assertRaises(RuntimeError):
                    chat("hello", session_id=self.session_id, is_admin=True)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["context_session"], self.session_id)
        self.assertIn("RuntimeError", seen[0]["error"])

    def test_memory_review_hooks_fire_when_batch_review_runs(self) -> None:
        from pupu.agent import REVIEW_INTERVAL, _maybe_batch_review

        seen = []
        register_hook(
            "memory.review_started",
            lambda event: seen.append((event.name, dict(event.payload))),
        )
        register_hook(
            "memory.review_finished",
            lambda event: seen.append((event.name, dict(event.payload))),
        )
        for index in range(REVIEW_INTERVAL):
            save_message("user", f"user-{index}", self.session_id, source=CHAT)
            save_message("assistant", f"assistant-{index}", self.session_id, source=CHAT)

        raw = """
        {
          "summary": "hook review summary",
          "fact_updates": [],
          "event_updates": [],
          "task_updates": []
        }
        """
        with patch("pupu.agent.is_memu_long_term_enabled", return_value=False):
            with patch("pupu.agent.find_related_person_facts", return_value=[]):
                with patch("pupu.agent.json_task", return_value=raw):
                    _maybe_batch_review(self.session_id)

        self.assertEqual([name for name, _payload in seen], ["memory.review_started", "memory.review_finished"])
        self.assertEqual(seen[0][1]["context_session"], self.session_id)
        self.assertEqual(seen[0][1]["trigger"], "messages")
        self.assertEqual(seen[1][1]["status"], "success")
        self.assertEqual(seen[1][1]["summary_chars"], len("hook review summary"))


if __name__ == "__main__":
    unittest.main()
