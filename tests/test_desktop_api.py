import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from pupu.actor import InstanceActor
from pupu.hooks import clear_hooks, emit_hook_sync
from pupu.instance_context import activate_instance_context
from pupu.logging_utils import close_all_log_sinks
from pupu.memory import init_db
from pupu_console import instance_store
from pupu_console.process_manager import DESKTOP_SESSION_ID, ProcessManager


class DesktopApiTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_hooks()
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

        from pupu_console import server

        self.server = server
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        clear_hooks()
        close_all_log_sinks()
        self.server.pm.stop_all()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _create_instance(self, name: str = "Desk") -> str:
        return instance_store.create_instance(name, qq_mode="cli", port=18141)

    def test_desktop_status_returns_instances(self) -> None:
        iid = self._create_instance()

        with self.client:
            response = self.client.get("/api/desktop/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["selected_instance_id"], iid)
        self.assertFalse(body["running"])
        self.assertEqual(body["session_id"], DESKTOP_SESSION_ID)
        self.assertEqual(body["instances"][0]["id"], iid)

    def test_desktop_chat_rejects_stopped_instance(self) -> None:
        iid = self._create_instance()

        with self.client:
            response = self.client.post(
                "/api/desktop/chat",
                json={"instance_id": iid, "text": "hello"},
            )

        self.assertEqual(response.status_code, 409)

    def test_desktop_chat_returns_reply_for_running_instance(self) -> None:
        iid = self._create_instance()

        with self.client:
            with patch.object(self.server.pm, "status", return_value={"running": True, "pid": 1, "runtime": "actor"}):
                with patch.object(self.server.pm, "desktop_chat", return_value="pong") as chat_mock:
                    response = self.client.post(
                        "/api/desktop/chat",
                        json={"instance_id": iid, "text": "hello"},
                    )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["instance_id"], iid)
        self.assertEqual(body["session_id"], DESKTOP_SESSION_ID)
        self.assertEqual(body["reply"], "pong")
        chat_mock.assert_awaited_once_with(iid, "hello")

    def test_desktop_events_websocket_receives_hook_events(self) -> None:
        with self.client:
            with self.client.websocket_connect("/ws/desktop/events") as ws:
                connected = ws.receive_json()
                self.assertEqual(connected["name"], "desktop.connected")
                emit_hook_sync("chat.started", {"context_session": DESKTOP_SESSION_ID})
                event = ws.receive_json()

        self.assertEqual(event["name"], "chat.started")
        self.assertEqual(event["payload"]["context_session"], DESKTOP_SESSION_ID)

    def test_hook_forwarder_does_not_replace_existing_hook_behavior(self) -> None:
        seen: list[str] = []

        from pupu.hooks import register_hook

        unregister = register_hook("chat.started", lambda event: seen.append(event.name))
        try:
            with self.client:
                emit_hook_sync("chat.started", {"context_session": DESKTOP_SESSION_ID})
        finally:
            unregister()

        self.assertEqual(seen, ["chat.started"])

    def test_multiple_napcat_instances_require_numeric_bot_id(self) -> None:
        first = instance_store.create_instance("A", qq_mode="napcat", port=18151)
        instance_store.create_instance("B", qq_mode="napcat", port=18152)

        with self.client:
            response = self.client.post(
                f"/api/instances/{first}/start",
                json={"qq_mode": "napcat"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Bot QQ / self_id", response.json()["detail"])


class DesktopProcessManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in ("PUPU_REPO_ROOT", "PUPU_YAML_PATH")
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmp.name
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

    async def test_desktop_chat_uses_running_actor_context(self) -> None:
        iid = instance_store.create_instance("Desk Actor", qq_mode="cli", port=18142)
        actor = InstanceActor.from_instance_dir(
            instance_store.instance_dir(iid),
            preflight=False,
            start_background_tasks=False,
        )
        with activate_instance_context(actor.context):
            init_db()
        actor._started = True

        pm = ProcessManager()
        pm._actors[iid] = actor

        captured: dict[str, object] = {}

        def fake_chat(text, session_id, is_admin, **kwargs):
            from pupu.instance_context import get_current_instance_context

            captured["text"] = text
            captured["session_id"] = session_id
            captured["is_admin"] = is_admin
            captured["context_session"] = kwargs.get("context_session")
            captured["identity_session"] = kwargs.get("identity_session")
            captured["instance_id"] = get_current_instance_context().instance_id
            return "desktop reply"

        with patch("pupu.agent.chat", fake_chat):
            reply = await pm.desktop_chat(iid, " hello ")

        self.assertEqual(reply, "desktop reply")
        self.assertEqual(captured["text"], "hello")
        self.assertEqual(captured["session_id"], DESKTOP_SESSION_ID)
        self.assertTrue(captured["is_admin"])
        self.assertEqual(captured["context_session"], DESKTOP_SESSION_ID)
        self.assertEqual(captured["identity_session"], DESKTOP_SESSION_ID)
        self.assertEqual(captured["instance_id"], iid)


if __name__ == "__main__":
    unittest.main()
