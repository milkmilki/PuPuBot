import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pupu import dialogue_loop
from pupu.actor import InstanceActor
from pupu.actor.onebot_transport import parse_onebot_message_segments
from pupu.instance_context import InstanceContext, activate_instance_context
from pupu.logging_utils import close_all_log_sinks
from pupu.memory import init_db
from pupu.sessions import OWNER_SESSION
from pupu_console import instance_store
from pupu_console.process_manager import ProcessManager


class OneBotParsingTests(unittest.TestCase):
    def test_parse_text_image_and_at_segments(self) -> None:
        text, images, ats = parse_onebot_message_segments(
            [
                {"type": "text", "data": {"text": "hi "}},
                {"type": "at", "data": {"qq": "123"}},
                {"type": "image", "data": {"url": "http://img"}},
                {"type": "mface", "data": {"url": "http://sticker"}},
            ]
        )
        self.assertEqual(text, "hi @123")
        self.assertEqual(images, ["http://img"])
        self.assertEqual(ats, ["123"])


class ActorMessageTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        close_all_log_sinks()

    def _make_ctx(self, root: Path, iid: str, *, port: int = 18081) -> InstanceContext:
        inst = root / "instances" / iid
        (inst / "data" / "logs").mkdir(parents=True)
        (inst / "instance.json").write_text(
            json.dumps(
                {
                    "id": iid,
                    "display_name": iid,
                    "qq_mode": "napcat",
                    "port": port,
                    "owner_ids": ["111"],
                    "private_reply_mode": "all",
                    "open_groups": ["900"],
                    "bot_id": iid,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (inst / "persona.json").write_text(
            json.dumps({"name": iid}, ensure_ascii=False),
            encoding="utf-8",
        )
        return InstanceContext.from_instance_dir(inst)

    async def test_slash_command_does_not_enter_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            actor = InstanceActor(ctx, preflight=False)
            sent: list[str] = []
            with activate_instance_context(ctx):
                init_db()
                actor.send_text = AsyncMock(side_effect=lambda _target, text: sent.append(text))
                with patch("pupu.actor.message_buffer.chat") as mock_chat:
                    await actor.handle_onebot_event(
                        {
                            "post_type": "message",
                            "message_type": "private",
                            "user_id": 111,
                            "message_id": 1,
                            "message": [{"type": "text", "data": {"text": "/help"}}],
                        }
                    )

            mock_chat.assert_not_called()
            self.assertTrue(any("/events" in item for item in sent))

    async def test_direct_onebot_handler_uses_actor_context_for_owner_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            actor = InstanceActor(ctx, preflight=False)
            actor.buffer._send_text = AsyncMock()
            with activate_instance_context(ctx):
                init_db()
            with patch("pupu.actor.message_buffer.chat", return_value="ok") as mock_chat:
                await actor.handle_onebot_event(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 111,
                        "message_id": 1,
                        "message": [{"type": "text", "data": {"text": "hello"}}],
                    }
                )
                await actor.buffer._process_buffer(
                    actor.buffer._buffers.pop("owner"),
                    persist_user=True,
                )

            mock_chat.assert_called_once()
            actor.buffer._send_text.assert_awaited_once()

    async def test_two_actor_buffers_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx_a = self._make_ctx(root, "actor-a", port=18081)
            ctx_b = self._make_ctx(root, "actor-b", port=18082)
            actor_a = InstanceActor(ctx_a, preflight=False)
            actor_b = InstanceActor(ctx_b, preflight=False)
            with activate_instance_context(ctx_a):
                init_db()
                await actor_a.buffer.handle(
                    actor_a._private_message("111", "A", "hello", [], {"message_id": 1})
                )
            with activate_instance_context(ctx_b):
                init_db()
                await actor_b.buffer.handle(
                    actor_b._private_message("222", "B", "world", [], {"message_id": 2})
                )

            self.assertIn("owner", actor_a.buffer._buffers)
            self.assertIn("private_222", actor_b.buffer._buffers)
            self.assertNotIn("private_222", actor_a.buffer._buffers)
            self.assertNotIn("owner", actor_b.buffer._buffers)
            await actor_a.buffer.stop()
            await actor_b.buffer.stop()

    async def test_open_group_arbiter_identity_falls_back_to_instance_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            actor = InstanceActor(ctx, preflight=False)
            captured: list[dict] = []

            async def fake_observe(payload: dict) -> dict:
                captured.append(payload)
                return {"ok": True, "latest_decision_id": 0}

            actor.buffer._post_observe = fake_observe
            actor.buffer._ensure_subscriber = lambda *_args, **_kwargs: None
            with activate_instance_context(ctx):
                init_db()
            await actor.handle_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 900,
                    "user_id": 111,
                    "message_id": 7,
                    "sender": {"nickname": "A"},
                    "message": [{"type": "text", "data": {"text": "hi"}}],
                }
            )

            self.assertEqual(captured[0]["reporter"]["bot_id"], "actor-a")

    async def test_open_group_arbiter_identity_prefers_connected_self_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            config = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            config["bot_id"] = ""
            ctx.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            actor = InstanceActor(ctx, preflight=False)
            actor.buffer._bot_qq_getter = lambda: "999001"
            captured: list[dict] = []

            async def fake_observe(payload: dict) -> dict:
                captured.append(payload)
                return {"ok": True, "latest_decision_id": 0}

            actor.buffer._post_observe = fake_observe
            actor.buffer._ensure_subscriber = lambda *_args, **_kwargs: None
            with activate_instance_context(ctx):
                init_db()
            await actor.handle_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 900,
                    "user_id": 111,
                    "message_id": 7,
                    "sender": {"nickname": "A"},
                    "message": [{"type": "text", "data": {"text": "hi"}}],
                }
            )

            self.assertEqual(captured[0]["reporter"]["bot_id"], "999001")
            self.assertEqual(captured[0]["reporter"]["qq"], "999001")

    async def test_proactive_owner_followup_sender_delivers_after_timer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            actor = InstanceActor(ctx, preflight=False)
            sent: list[tuple[object, str]] = []

            async def fake_proactive_loop(_send_func):
                await asyncio.sleep(3600)

            async def fake_send_text(target, text: str) -> None:
                sent.append((target, text))

            actor.send_text = AsyncMock(side_effect=fake_send_text)
            with activate_instance_context(ctx):
                init_db()
                with patch("pupu.actor.instance_actor.proactive_loop", side_effect=fake_proactive_loop):
                    actor._start_proactive_loop()
            try:
                with patch("pupu.agent.chat", return_value="追问一下") as mock_chat:
                    dialogue_loop._on_timer_fire(OWNER_SESSION)
                    await asyncio.sleep(0.05)

                self.assertEqual([text for _target, text in sent], ["追问一下"])
                self.assertEqual(sent[0][0].session_id, OWNER_SESSION)
                self.assertIn("[系统触发的追问]", mock_chat.call_args.args[0])
                self.assertEqual(mock_chat.call_args.kwargs["message_source"], "wait_followup")
            finally:
                actor._stop_proactive_loop()
                for task in list(actor._tasks):
                    task.cancel()
                if actor._tasks:
                    await asyncio.gather(*actor._tasks, return_exceptions=True)
                dialogue_loop.unregister_sender(OWNER_SESSION)

    async def test_actor_start_respects_proactive_enabled_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            cfg = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            cfg["proactive_enabled"] = False
            ctx.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            actor = InstanceActor(ctx, preflight=False)

            with patch("pupu.actor.instance_actor.OneBotTransport.start", new_callable=AsyncMock):
                with patch("pupu.actor.instance_actor.OneBotTransport.stop", new_callable=AsyncMock):
                    with patch.object(actor, "_start_proactive_loop", return_value="started") as mock_start:
                        await actor.start()
                        await actor.stop()

            mock_start.assert_not_called()

    async def test_actor_start_respects_proactive_enabled_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            cfg = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            cfg["proactive_enabled"] = True
            ctx.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            actor = InstanceActor(ctx, preflight=False)

            with patch("pupu.actor.instance_actor.OneBotTransport.start", new_callable=AsyncMock):
                with patch("pupu.actor.instance_actor.OneBotTransport.stop", new_callable=AsyncMock):
                    with patch.object(actor, "_start_proactive_loop", return_value="started") as mock_start:
                        await actor.start()
                        await actor.stop()

            mock_start.assert_called_once()


class OneBotTransportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PUPU_REPO_ROOT",
                "PUPU_YAML_PATH",
                "PUPU_CODEX_MCP_SERVERS_JSON",
                "PUPU_MCP_SERVERS_JSON",
            )
        }
        root = Path(self._tmp.name)
        os.environ["PUPU_REPO_ROOT"] = str(root)
        os.environ["PUPU_YAML_PATH"] = str(root / "pupu.yaml")
        os.environ.pop("PUPU_CODEX_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
        (root / "pupu.yaml").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        close_all_log_sinks()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _make_ctx(self, iid: str, *, port: int) -> InstanceContext:
        inst = Path(self._tmp.name) / "instances" / iid
        (inst / "data" / "logs").mkdir(parents=True)
        (inst / "instance.json").write_text(
            json.dumps(
                {
                    "id": iid,
                    "display_name": iid,
                    "qq_mode": "napcat",
                    "port": port,
                    "owner_ids": ["111"],
                    "private_reply_mode": "all",
                    "open_groups": [],
                    "bot_id": iid,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (inst / "persona.json").write_text(
            json.dumps({"name": iid}, ensure_ascii=False),
            encoding="utf-8",
        )
        return InstanceContext.from_instance_dir(inst)

    async def test_actor_onebot_reverse_ws_accepts_napcat_and_echo_actions(self) -> None:
        try:
            import websockets
        except ImportError:
            self.skipTest("websockets is not installed")

        port = self._free_port()
        ctx = self._make_ctx("actor-onebot", port=port)
        actor = InstanceActor(ctx, preflight=False, start_background_tasks=False)
        inbound: list[dict] = []

        async def capture_event(event: dict) -> None:
            inbound.append(event)

        actor.handle_onebot_event = capture_event
        await actor.start()
        try:
            uri = f"ws://127.0.0.1:{port}/onebot/v11/ws?self_id=999001"
            async with websockets.connect(uri, additional_headers={"x-self-id": "999001"}) as ws:
                for _ in range(20):
                    if actor.transport and actor.transport.info.connected:
                        break
                    await asyncio.sleep(0.05)
                self.assertIsNotNone(actor.transport)
                self.assertTrue(actor.transport.info.connected)
                self.assertEqual(actor.transport.info.self_id, "999001")

                login_task = asyncio.create_task(actor.transport.get_login_info())
                action = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                self.assertEqual(action["action"], "get_login_info")
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"user_id": 999001, "nickname": "actor-test"},
                            "echo": action["echo"],
                        }
                    )
                )
                self.assertEqual(
                    await asyncio.wait_for(login_task, timeout=5),
                    {"user_id": 999001, "nickname": "actor-test"},
                )

                await ws.send(
                    json.dumps(
                        {
                            "post_type": "message",
                            "message_type": "private",
                            "user_id": 111,
                            "message_id": 42,
                            "message": [{"type": "text", "data": {"text": "/help"}}],
                        }
                    )
                )
                for _ in range(20):
                    if inbound:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(inbound[0]["message_type"], "private")
        finally:
            await actor.stop()


class ProcessManagerActorModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PUPU_REPO_ROOT",
                "PUPU_YAML_PATH",
                "PUPU_CODEX_MCP_SERVERS_JSON",
                "PUPU_MCP_SERVERS_JSON",
            )
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmp.name
        os.environ.pop("PUPU_CODEX_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
        yaml_path = Path(self._tmp.name) / "pupu.yaml"
        yaml_path.write_text("", encoding="utf-8")
        os.environ["PUPU_YAML_PATH"] = str(yaml_path)

    def tearDown(self) -> None:
        close_all_log_sinks()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def test_actor_mode_does_not_popen(self) -> None:
        iid = instance_store.create_instance("A", qq_mode="cli", port=18101)
        pm = ProcessManager()
        pm.set_event_loop(asyncio.get_running_loop())
        with patch("pupu.actor.instance_actor.preflight_model_providers"):
            original = InstanceActor.from_instance_dir
            with patch(
                "pupu_console.process_manager.InstanceActor.from_instance_dir",
                side_effect=lambda *args, **kwargs: original(
                    *args,
                    **{**kwargs, "start_background_tasks": False},
                ),
            ):
                pid = await asyncio.to_thread(pm.start, iid)
        self.assertEqual(pid, os.getpid())
        status = pm.status(iid)
        self.assertTrue(status["running"])
        self.assertEqual(status["runtime"], "actor")
        await asyncio.to_thread(pm.stop, iid)
        self.assertFalse(pm.status(iid)["running"])


if __name__ == "__main__":
    unittest.main()
