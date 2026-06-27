import os
import tempfile
import unittest
import yaml
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

    def test_debug_smoke_send_text_requires_loopback(self) -> None:
        iid = self._create_instance()

        with TestClient(self.server.app, client=("203.0.113.9", 4321)) as remote_client:
            response = remote_client.post(
                "/api/debug/smoke/send_text",
                json={
                    "instance_id": iid,
                    "target": "group",
                    "group_id": "900",
                    "text": "hello",
                },
            )

        self.assertEqual(response.status_code, 403)

    def test_debug_smoke_send_text_routes_to_process_manager(self) -> None:
        iid = self._create_instance()

        with self.client:
            with patch.object(self.server.pm, "smoke_send_text", return_value={"ok": True}) as send_mock:
                response = self.client.post(
                    "/api/debug/smoke/send_text",
                    json={
                        "instance_id": iid,
                        "target": "group",
                        "group_id": "900",
                        "text": "hello",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        send_mock.assert_awaited_once_with(
            iid,
            target="group",
            text="hello",
            user_id="",
            group_id="900",
        )

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

    def test_start_instance_preserves_siri_launch_mode(self) -> None:
        iid = instance_store.create_instance("Desk", qq_mode="napcat", port=18153)

        with self.client:
            with patch.object(self.server.pm, "start", return_value=1234):
                response = self.client.post(
                    f"/api/instances/{iid}/start",
                    json={"qq_mode": "siri"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["qq_mode"], "siri")
        cfg, _ = instance_store.read_instance_files(iid)
        self.assertEqual(cfg["qq_mode"], "siri")

    def test_mcp_settings_masks_secrets_and_lists_builtin_media(self) -> None:
        yaml_path = Path(os.environ["PUPU_YAML_PATH"])
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "semantic_index": {
                        "embed_api_key": "dashscope-secret",
                        "embed_base_url": "https://dashscope.test/compatible-mode/v1",
                    },
                    "vision": {"model": "qwen3.6-flash", "timeout": 45},
                    "mcp": {
                        "servers": {
                            "tavily": {
                                "enabled": True,
                                "command": "cmd",
                                "args": ["/c", "npx", "-y", "tavily-mcp@latest"],
                                "env": {"TAVILY_API_KEY": "tavily-secret"},
                            }
                        }
                    },
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with self.client:
            response = self.client.get("/api/desktop/settings/mcp")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        media = next(item for item in body["builtin_servers"] if item["id"] == "media")
        self.assertTrue(media["enabled"])
        self.assertTrue(any(tool["name"] == "mcp__media__describe_image" for tool in media["tools"]))
        key_field = next(field for field in media["config_fields"] if field["key"] == "semantic_index.embed_api_key")
        self.assertEqual(key_field["value"], "")
        self.assertTrue(key_field["secret"]["has_value"])
        self.assertNotIn("dashscope-secret", str(body))
        tavily = next(item for item in body["external_servers"] if item["id"] == "tavily")
        self.assertEqual(tavily["env"][0]["value"], "")
        self.assertTrue(tavily["env"][0]["secret"]["has_value"])
        self.assertNotIn("tavily-secret", str(body))

    def test_mcp_settings_get_requires_loopback(self) -> None:
        with TestClient(self.server.app, client=("203.0.113.9", 4321)) as remote_client:
            response = remote_client.get("/api/desktop/settings/mcp")

        self.assertEqual(response.status_code, 403)

    def test_mcp_settings_put_updates_builtin_and_external_config(self) -> None:
        with self.client:
            response = self.client.put(
                "/api/desktop/settings/mcp",
                json={
                    "builtin_servers": [{"id": "media", "enabled": False}],
                    "values": {
                        "vision.model": "qwen-test",
                        "semantic_index.embed_api_key": "new-secret",
                    },
                    "external_servers": [
                        {
                            "id": "demo-search",
                            "enabled": True,
                            "command": "python",
                            "args": ["-m", "demo"],
                            "exposures": ["chat"],
                            "env": [{"name": "DEMO_API_KEY", "value": "demo-secret"}],
                        },
                        {
                            "id": "tavily",
                            "preset": True,
                            "enabled": True,
                            "env": [{"name": "TAVILY_API_KEY", "value": "tavily-secret"}],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        cfg = yaml.safe_load(Path(os.environ["PUPU_YAML_PATH"]).read_text(encoding="utf-8"))
        self.assertFalse(cfg["tool_servers"]["media"]["enabled"])
        self.assertEqual(cfg["vision"]["model"], "qwen-test")
        self.assertEqual(cfg["semantic_index"]["embed_api_key"], "new-secret")
        self.assertEqual(cfg["mcp"]["servers"]["demo-search"]["env"]["DEMO_API_KEY"], "demo-secret")
        self.assertEqual(cfg["mcp"]["servers"]["tavily"]["env"]["TAVILY_API_KEY"], "tavily-secret")
        self.assertEqual(cfg["mcp"]["servers"]["tavily"]["command"], "cmd")

    def test_mcp_settings_refresh_applies_builtin_disable_to_registry(self) -> None:
        yaml_path = Path(os.environ["PUPU_YAML_PATH"])
        yaml_path.write_text(
            yaml.safe_dump({"tool_servers": {"media": {"enabled": False}}}, allow_unicode=True),
            encoding="utf-8",
        )

        with self.client:
            response = self.client.post("/api/desktop/settings/mcp/refresh")

        self.assertEqual(response.status_code, 200)
        from pupu.tools import get_chat_tool_definitions

        names = {tool["name"] for tool in get_chat_tool_definitions()}
        self.assertNotIn("mcp__media__describe_image", names)

    def test_mcp_settings_test_reports_external_error_without_secret(self) -> None:
        yaml_path = Path(os.environ["PUPU_YAML_PATH"])
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "mcp": {
                        "servers": {
                            "bad": {
                                "enabled": True,
                                "command": "definitely-not-a-real-mcp-command",
                                "env": {"BAD_API_KEY": "super-secret-token"},
                            }
                        }
                    }
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with self.client:
            response = self.client.post(
                "/api/desktop/settings/mcp/test",
                json={"server_id": "bad"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body)
        self.assertNotIn("super-secret-token", str(body))

    def test_mcp_settings_test_redacts_secret_from_loader_error(self) -> None:
        yaml_path = Path(os.environ["PUPU_YAML_PATH"])
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "mcp": {
                        "servers": {
                            "bad": {
                                "enabled": True,
                                "command": "fake-command",
                                "env": {"BAD_API_KEY": "super-secret-token"},
                            }
                        }
                    }
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        class FakeMcpServer:
            def __init__(self, config):
                self.config = config

            def list_tools(self):
                raise RuntimeError(f"loader echoed {self.config['env']['BAD_API_KEY']}")

            def close(self):
                pass

        with self.client:
            with patch("pupu_console.mcp_settings.ExternalMcpToolServer", FakeMcpServer):
                response = self.client.post(
                    "/api/desktop/settings/mcp/test",
                    json={"server_id": "bad"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("[secret]", body["error"])
        self.assertNotIn("super-secret-token", str(body))

    def test_mcp_settings_delete_external_removes_configured_card(self) -> None:
        yaml_path = Path(os.environ["PUPU_YAML_PATH"])
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "mcp": {
                        "servers": {
                            "demo": {
                                "enabled": True,
                                "command": "python",
                                "env": {"DEMO_API_KEY": "demo-secret"},
                            }
                        }
                    }
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with self.client:
            response = self.client.put(
                "/api/desktop/settings/mcp",
                json={"delete_external": ["demo"]},
            )

        self.assertEqual(response.status_code, 200)
        cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        self.assertNotIn("demo", cfg.get("mcp", {}).get("servers", {}))
        ids = {item["id"] for item in response.json()["external_servers"]}
        self.assertNotIn("demo", ids)


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

    async def test_smoke_send_text_uses_running_actor_context(self) -> None:
        iid = instance_store.create_instance("Desk Actor", qq_mode="cli", port=18143)
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

        async def fake_send_text(target, text: str) -> None:
            from pupu.instance_context import get_current_instance_context

            captured["target"] = target
            captured["text"] = text
            captured["instance_id"] = get_current_instance_context().instance_id

        actor.send_text = fake_send_text

        result = await pm.smoke_send_text(
            iid,
            target="group",
            group_id="900",
            text=" hello ",
        )

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["group_id"], "900")
        self.assertEqual(captured["text"], "hello")
        self.assertEqual(captured["target"].session_id, "group_900")
        self.assertEqual(captured["target"].group_id, "900")
        self.assertEqual(captured["instance_id"], iid)


if __name__ == "__main__":
    unittest.main()
