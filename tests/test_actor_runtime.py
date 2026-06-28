import asyncio
from concurrent.futures import Future
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pupu import dialogue_loop
from pupu.actor import InstanceActor
from pupu.actor.message_buffer import MessageBuffer, _Buffer
from pupu.actor.onebot_transport import OneBotTransport, parse_onebot_message_segments
from pupu.actor.types import ActorInboundMessage, ActorOutboundTarget
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
                {"type": "mface", "data": {"url": "http://sticker", "summary": "🤫"}},
            ]
        )
        self.assertEqual(text, "hi @123[sticker: 🤫]")
        self.assertEqual(images, ["http://img"])
        self.assertEqual(ats, ["123"])

    def test_parse_standalone_stickers_as_text(self) -> None:
        text, images, ats = parse_onebot_message_segments(
            [
                {"type": "mface", "data": {"summary": "🤫"}},
                {"type": "image", "data": {"subType": 1, "summary": "表情"}},
                {"type": "face", "data": {"id": 178}},
            ]
        )

        self.assertEqual(text, "[sticker: 🤫][sticker: 表情][emoji 178]")
        self.assertEqual(images, [])
        self.assertEqual(ats, [])


class OneBotTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_releases_bound_port(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        async def _handle(_event: dict) -> None:
            return None

        transport = OneBotTransport(
            host="127.0.0.1",
            port=port,
            on_event=_handle,
        )
        await transport.start()
        await transport.stop()

        rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            rebound.bind(("127.0.0.1", port))
        finally:
            rebound.close()

    async def test_start_fails_when_port_is_already_bound(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        logs: list[str] = []

        async def _handle(_event: dict) -> None:
            return None

        transport = OneBotTransport(
            host="127.0.0.1",
            port=port,
            on_event=_handle,
            log=logs.append,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                await transport.start()
        finally:
            blocker.close()
            await transport.stop()

        self.assertFalse(any("reverse WebSocket listening" in line for line in logs))


class ActorMessageTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        close_all_log_sinks()

    def _make_ctx(
        self,
        root: Path,
        iid: str,
        *,
        port: int = 18081,
        qq_mode: str = "napcat",
        owner_ids: list[str] | None = None,
    ) -> InstanceContext:
        inst = root / "instances" / iid
        (inst / "data" / "logs").mkdir(parents=True)
        (inst / "instance.json").write_text(
            json.dumps(
                {
                    "id": iid,
                    "display_name": iid,
                    "qq_mode": qq_mode,
                    "port": port,
                    "owner_ids": ["111"] if owner_ids is None else owner_ids,
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

    async def test_standalone_sticker_message_stays_in_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a")
            actor = InstanceActor(ctx, preflight=False)
            with activate_instance_context(ctx):
                init_db()
            await actor.handle_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 111,
                    "message_id": 1,
                    "message": [{"type": "text", "data": {"text": "不是分镜啦"}}],
                }
            )
            await actor.handle_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 111,
                    "message_id": 2,
                    "message": [{"type": "mface", "data": {"summary": "🤫"}}],
                }
            )
            await actor.handle_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 111,
                    "message_id": 3,
                    "message": [{"type": "text", "data": {"text": "姐姐先让我安静画一会"}}],
                }
            )

            buf = actor.buffer._buffers["owner"]
            self.assertEqual(
                buf.texts,
                ["不是分镜啦", "[sticker: 🤫]", "姐姐先让我安静画一会"],
            )
            await actor.buffer.stop()

    async def test_debounce_flush_logs_processing_errors_without_leaking_task_exception(self) -> None:
        logs: list[str] = []
        buffer = MessageBuffer(
            send_text=AsyncMock(),
            handle_command=AsyncMock(return_value=False),
            log=logs.append,
            debounce_seconds=0,
        )
        message = ActorInboundMessage(
            session_id="owner",
            identity_session=OWNER_SESSION,
            user_id="111",
            user_name="Owner",
            text="🤫",
            message_id="emoji-msg",
        )
        buffer._buffers["owner"] = _Buffer(message=message, texts=["🤫"])

        error = UnicodeEncodeError("gbk", "🤫", 0, 1, "illegal multibyte sequence")
        with patch.object(buffer, "_process_buffer", side_effect=error):
            await buffer._debounce_flush("owner")

        self.assertTrue(any("debounce flush error session=owner" in line for line in logs))
        self.assertNotIn("owner", buffer._session_phase)
        self.assertNotIn("owner", buffer._debounce_tasks)

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

    async def test_arbiter_subscriber_advances_cursor_to_observe_latest_decision(self) -> None:
        buffer = MessageBuffer(
            send_text=AsyncMock(),
            handle_command=AsyncMock(return_value=False),
        )
        buffer._arbiter_last_decision_id["900"] = 3

        class DummyTask:
            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                return None

        def fake_create_task(coro):
            coro.close()
            return DummyTask()

        with patch.object(buffer, "is_group_silenced", return_value=False):
            with patch("pupu.actor.message_buffer.asyncio.create_task", side_effect=fake_create_task):
                buffer._ensure_subscriber("900", "group_900", 12)

        self.assertEqual(buffer._arbiter_last_decision_id["900"], 12)

    async def test_arbiter_non_selected_decision_clears_open_group_buffer(self) -> None:
        buffer = MessageBuffer(
            send_text=AsyncMock(),
            handle_command=AsyncMock(return_value=False),
            bot_qq_getter=lambda: "bot-a",
        )
        message = ActorInboundMessage(
            session_id="group_900",
            identity_session="owner",
            user_id="111",
            user_name="User",
            text="hello",
            group_id="900",
            message_id="msg-1",
        )
        buffer._buffers["group_900"] = _Buffer(
            message=message,
            texts=["hello"],
            is_open_group=True,
        )

        class FakeRuntime:
            def __init__(self) -> None:
                self.silence_checks = 0

            def is_silenced(self, _group_id: str) -> bool:
                self.silence_checks += 1
                return self.silence_checks >= 2

            async def await_decision(self, _group_id: str, _since: int, *, timeout: float):
                return {
                    "decision_id": 7,
                    "speaker": "none",
                    "reason": "test_none",
                    "confidence": 1.0,
                }

        with patch("pupu.actor.message_buffer.load_bot_id", return_value="bot-a"):
            with patch("pupu.actor.message_buffer.get_shared_arbiter_runtime", return_value=FakeRuntime()):
                await buffer._arbiter_decision_subscriber("900", "group_900")

        self.assertNotIn("group_900", buffer._buffers)
        self.assertNotIn("group_900", buffer._session_phase)

    async def test_arbiter_stale_decision_does_not_clear_newer_open_group_buffer(self) -> None:
        buffer = MessageBuffer(
            send_text=AsyncMock(),
            handle_command=AsyncMock(return_value=False),
            bot_qq_getter=lambda: "bot-a",
        )
        message = ActorInboundMessage(
            session_id="group_900",
            identity_session="owner",
            user_id="111",
            user_name="User",
            text="newer message",
            group_id="900",
            message_id="new-msg",
        )
        buffer._buffers["group_900"] = _Buffer(
            message=message,
            texts=["newer message"],
            is_open_group=True,
        )

        class FakeRuntime:
            def __init__(self) -> None:
                self.silence_checks = 0

            def is_silenced(self, _group_id: str) -> bool:
                self.silence_checks += 1
                return self.silence_checks >= 2

            async def await_decision(self, _group_id: str, _since: int, *, timeout: float):
                return {
                    "decision_id": 8,
                    "speaker": "none",
                    "reason": "old_none",
                    "confidence": 1.0,
                    "since_message_id": "old-msg",
                }

        with patch("pupu.actor.message_buffer.load_bot_id", return_value="bot-a"):
            with patch("pupu.actor.message_buffer.get_shared_arbiter_runtime", return_value=FakeRuntime()):
                await buffer._arbiter_decision_subscriber("900", "group_900")

        self.assertIn("group_900", buffer._buffers)
        self.assertEqual(buffer._buffers["group_900"].message.message_id, "new-msg")

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

    async def test_actor_start_returns_before_background_tasks_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a", qq_mode="cli")
            cfg = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            cfg["proactive_enabled"] = False
            ctx.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            actor = InstanceActor(ctx, preflight=False)

            async def blocking_background_loop() -> None:
                time.sleep(0.5)

            actor._scheduler_loop = blocking_background_loop  # type: ignore[method-assign]
            actor._maintenance_loop = blocking_background_loop  # type: ignore[method-assign]

            started_at = time.perf_counter()
            await actor.start()
            elapsed = time.perf_counter() - started_at

            for task in list(actor._tasks):
                task.cancel()
            if actor._tasks:
                await asyncio.gather(*actor._tasks, return_exceptions=True)
            await actor.stop()

            self.assertLess(elapsed, 2.0)

    async def test_cli_actor_start_respects_proactive_enabled_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a", qq_mode="cli", owner_ids=[])
            cfg = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            cfg["proactive_enabled"] = True
            ctx.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            actor = InstanceActor(ctx, preflight=False)

            with patch.object(actor, "_start_proactive_loop", return_value="started") as mock_start:
                await actor.start()
                await actor.stop()

            mock_start.assert_called_once()

    async def test_cli_proactive_does_not_require_owner_qq_and_prints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a", qq_mode="cli", owner_ids=[])
            delivered: list[str] = []
            actor = InstanceActor(ctx, preflight=False, cli_send=delivered.append)

            async def fake_proactive_loop(send_func):
                await send_func("主动问一句")
                await asyncio.sleep(3600)

            with activate_instance_context(ctx):
                init_db()
                with patch("pupu.actor.instance_actor.proactive_loop", side_effect=fake_proactive_loop):
                    result = actor._start_proactive_loop()
                    await asyncio.sleep(0.05)
            try:
                self.assertEqual(result, "主动消息已开启，后台循环已启动。")
                self.assertEqual(delivered, ["主动问一句"])
            finally:
                actor._stop_proactive_loop()
                for task in list(actor._tasks):
                    task.cancel()
                if actor._tasks:
                    await asyncio.gather(*actor._tasks, return_exceptions=True)
                dialogue_loop.unregister_sender(OWNER_SESSION)

    async def test_cli_proactive_followup_sender_prints_after_timer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a", qq_mode="cli", owner_ids=[])
            delivered: list[str] = []
            actor = InstanceActor(ctx, preflight=False, cli_send=delivered.append)

            async def fake_proactive_loop(_send_func):
                await asyncio.sleep(3600)

            with activate_instance_context(ctx):
                init_db()
                with patch("pupu.actor.instance_actor.proactive_loop", side_effect=fake_proactive_loop):
                    actor._start_proactive_loop()
            try:
                with patch("pupu.agent.chat", return_value="追问一下"):
                    dialogue_loop._on_timer_fire(OWNER_SESSION)
                    await asyncio.sleep(0.05)

                self.assertEqual(delivered, ["追问一下"])
            finally:
                actor._stop_proactive_loop()
                for task in list(actor._tasks):
                    task.cancel()
                if actor._tasks:
                    await asyncio.gather(*actor._tasks, return_exceptions=True)
                dialogue_loop.unregister_sender(OWNER_SESSION)

    async def test_proactive_force_command_sends_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._make_ctx(Path(tmp), "actor-a", qq_mode="cli", owner_ids=[])
            delivered: list[str] = []
            actor = InstanceActor(ctx, preflight=False, cli_send=delivered.append)

            with activate_instance_context(ctx):
                init_db()
                with patch("pupu.actor.instance_actor._get_current_period", return_value={"name": "白天"}):
                    with patch("pupu.actor.instance_actor.generate_proactive_message", return_value="主动测试"):
                        should_exit = await actor.handle_cli_text("proactive force", delivered.append)

            self.assertFalse(should_exit)
            self.assertIn("主动测试", delivered)
            self.assertIn("主动消息 force 已发送。", delivered)


class OneBotTransportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PUPU_REPO_ROOT",
                "PUPU_YAML_PATH",
                "PUPU_MCP_SERVERS_JSON",
            )
        }
        root = Path(self._tmp.name)
        os.environ["PUPU_REPO_ROOT"] = str(root)
        os.environ["PUPU_YAML_PATH"] = str(root / "pupu.yaml")
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

    async def test_actor_onebot_command_send_can_receive_echo_while_handling_event(self) -> None:
        try:
            import websockets
        except ImportError:
            self.skipTest("websockets is not installed")

        port = self._free_port()
        ctx = self._make_ctx("actor-onebot-command", port=port)
        actor = InstanceActor(ctx, preflight=False, start_background_tasks=False)
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

                await ws.send(
                    json.dumps(
                        {
                            "post_type": "message",
                            "message_type": "private",
                            "user_id": 111,
                            "message_id": 43,
                            "message": [{"type": "text", "data": {"text": "proactive status"}}],
                        }
                    )
                )
                action = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                self.assertEqual(action["action"], "send_private_msg")
                self.assertEqual(action["params"]["user_id"], 111)
                self.assertIn("主动消息", action["params"]["message"])
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 1001},
                            "echo": action["echo"],
                        }
                    )
                )

                for _ in range(20):
                    if not actor.transport or not actor.transport._pending:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(actor.transport._pending, {})
                self.assertTrue(actor.transport.info.connected)
        finally:
            await actor.stop()

    async def test_actor_onebot_rejects_unexpected_self_id(self) -> None:
        try:
            import websockets
        except Exception:  # pragma: no cover - optional dependency in minimal envs
            self.skipTest("websockets not installed")

        port = self._free_port()
        ctx = self._make_ctx("actor-onebot-guard", port=port)
        config = json.loads(ctx.config_path.read_text(encoding="utf-8"))
        config["bot_id"] = "999001"
        ctx.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        actor = InstanceActor(ctx, preflight=False, start_background_tasks=False)
        logs: list[str] = []
        actor._emit_log = logs.append

        await actor.start()
        try:
            uri = f"ws://127.0.0.1:{port}/onebot/v11/ws?self_id=888002"
            with self.assertRaises(Exception):
                async with websockets.connect(uri, additional_headers={"x-self-id": "888002"}) as ws:
                    await ws.recv()

            self.assertIsNotNone(actor.transport)
            self.assertFalse(actor.transport.info.connected)
            self.assertTrue(any("unexpected self_id" in line for line in logs))
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
                "PUPU_MCP_SERVERS_JSON",
            )
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmp.name
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

    async def test_status_preserves_actor_while_start_future_pending(self) -> None:
        iid = instance_store.create_instance("Starting", qq_mode="napcat", port=18109)
        pm = ProcessManager()
        actor = InstanceActor.from_instance_dir(instance_store.instance_dir(iid), preflight=False)
        future: Future[None] = Future()

        pm._actors[iid] = actor
        pm._actor_tasks[iid] = future

        status = pm.status(iid)

        self.assertTrue(status["running"])
        self.assertEqual(status.get("state"), "starting")
        self.assertIs(pm._actors.get(iid), actor)
        future.cancel()

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

    async def test_siri_actor_mode_starts_without_onebot_transport(self) -> None:
        iid = instance_store.create_instance("Desk", qq_mode="siri", port=18102)
        pm = ProcessManager()
        pm.set_event_loop(asyncio.get_running_loop())
        delivered: list[str] = []
        with patch("pupu.actor.instance_actor.preflight_model_providers"):
            original = InstanceActor.from_instance_dir
            with patch(
                "pupu_console.process_manager.InstanceActor.from_instance_dir",
                side_effect=lambda *args, **kwargs: original(
                    *args,
                    **{
                        **kwargs,
                        "cli_send": delivered.append,
                        "start_background_tasks": False,
                    },
                ),
            ):
                pid = await asyncio.to_thread(pm.start, iid)

        self.assertEqual(pid, os.getpid())
        actor = pm.get_actor(iid)
        self.assertIsNotNone(actor)
        self.assertIsNone(actor.transport)
        await actor.send_text(ActorOutboundTarget(session_id="desktop_owner"), "hello")
        self.assertEqual(delivered, ["hello"])
        await asyncio.to_thread(pm.stop, iid)


if __name__ == "__main__":
    unittest.main()
