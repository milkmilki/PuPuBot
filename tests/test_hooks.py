import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pupu.actor import InstanceActor
from pupu.hooks import clear_hooks, list_hooks, register_hook
from pupu.instance_context import InstanceContext, activate_instance_context
from pupu.memory import init_db


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


if __name__ == "__main__":
    unittest.main()
