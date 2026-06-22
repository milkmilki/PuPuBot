import os
from pathlib import Path
import unittest
from tests.helpers import activate_test_instance
from unittest.mock import patch
import sys
import json

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.memory import (
    cancel_scheduled_task,
    create_scheduled_task,
    init_db,
    list_scheduled_tasks,
    reset_session,
)
from pupu.agent import REVIEW_INTERVAL
import pupu.tools as tools
from pupu.tools import (
    describe_tool_servers,
    execute_tool,
    get_chat_tool_definitions,
    is_admin_tool,
    refresh_tool_definitions,
)
from pupu.tooling import refresh_registry


class ToolingRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        reset_session("test_tooling_registry")
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_CODEX_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_VISION_MODEL", None)
        os.environ.pop("PUPU_VISION_TIMEOUT", None)
        os.environ.pop("PUPU_MEMU_EMBED_API_KEY", None)
        os.environ.pop("PUPU_MEMU_EMBED_BASE_URL", None)
        os.environ.pop("PUPU_MEMU_API_KEY", None)
        os.environ.pop("PUPU_MEMU_BASE_URL", None)
        refresh_tool_definitions()

    def tearDown(self):
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_CODEX_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_VISION_MODEL", None)
        os.environ.pop("PUPU_VISION_TIMEOUT", None)
        os.environ.pop("PUPU_MEMU_EMBED_API_KEY", None)
        os.environ.pop("PUPU_MEMU_EMBED_BASE_URL", None)
        os.environ.pop("PUPU_MEMU_API_KEY", None)
        os.environ.pop("PUPU_MEMU_BASE_URL", None)
        refresh_tool_definitions()

    def test_chat_tools_are_namespaced(self):
        names = {tool["name"] for tool in tools.TOOL_DEFINITIONS}
        self.assertNotIn("mcp__web__search", names)
        self.assertIn("mcp__scheduler__manage_scheduled_task", names)
        self.assertNotIn("web_search", names)

    def test_admin_flags_work_for_canonical_and_legacy_names(self):
        self.assertTrue(is_admin_tool("read_file"))
        self.assertTrue(is_admin_tool("mcp__filesystem__read_file"))
        self.assertTrue(is_admin_tool("mcp__system__run_command"))
        self.assertFalse(is_admin_tool("mcp__scheduler__manage_scheduled_task"))

    def test_proactive_tools_are_filtered(self):
        names = {tool["name"] for tool in tools.PROACTIVE_TOOL_DEFINITIONS}
        self.assertEqual(names, set())

    def test_legacy_dispatch_still_works(self):
        result = execute_tool(
            "manage_scheduled_task",
            {"action": "list"},
            session_id="test_tooling_registry",
        )
        self.assertIn("当前没有待执行的定时任务", result)
        self.assertIn(
            f"总结进度：0/{REVIEW_INTERVAL}，还差 {REVIEW_INTERVAL} 条消息触发自动总结",
            result,
        )

    def test_tool_reason_hint_is_hidden_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUPU_DEBUG_TOOL_REASON", None)
            with patch("builtins.print") as mock_print:
                execute_tool(
                    "mcp__scheduler__manage_scheduled_task",
                    {"action": "list"},
                    session_id="test_tooling_registry",
                    reason_hint="我先搜搜看",
                )

        first_line = str(mock_print.call_args_list[0].args[0])
        self.assertIn("input=", first_line)
        self.assertNotIn("reason=", first_line)
        self.assertNotIn("我先搜搜看", "\n".join(str(call.args[0]) for call in mock_print.call_args_list))

    def test_tool_reason_hint_can_be_enabled_for_deep_debug(self):
        with patch.dict(os.environ, {"PUPU_DEBUG_TOOL_REASON": "1"}, clear=False):
            with patch("builtins.print") as mock_print:
                execute_tool(
                    "mcp__scheduler__manage_scheduled_task",
                    {"action": "list"},
                    session_id="test_tooling_registry",
                    reason_hint="我先搜搜看",
                )

        first_line = str(mock_print.call_args_list[0].args[0])
        self.assertIn("reason=我先搜搜看", first_line)

    def test_scheduled_task_list_uses_display_indices(self):
        first_id = create_scheduled_task(
            "test_tooling_registry",
            "first",
            "first instruction",
            "2026-05-01T10:00:00",
            "once",
            None,
        )
        second_id = create_scheduled_task(
            "test_tooling_registry",
            "second",
            "second instruction",
            "2026-05-01T11:00:00",
            "once",
            None,
        )
        cancel_scheduled_task("test_tooling_registry", first_id)

        result = execute_tool(
            "manage_scheduled_task",
            {"action": "list"},
            session_id="test_tooling_registry",
        )

        self.assertIn(f"#1 id={second_id}", result)
        self.assertNotIn("#2", result)

    def test_scheduled_task_cancel_supports_display_index(self):
        first_id = create_scheduled_task(
            "test_tooling_registry",
            "first",
            "first instruction",
            "2026-05-01T09:00:00",
            "once",
            None,
        )
        second_id = create_scheduled_task(
            "test_tooling_registry",
            "second",
            "second instruction",
            "2026-05-01T10:00:00",
            "once",
            None,
        )

        result = execute_tool(
            "manage_scheduled_task",
            {"action": "cancel", "task_index": 1},
            session_id="test_tooling_registry",
        )
        remaining = list_scheduled_tasks("test_tooling_registry")

        self.assertIn(f"#1 id={first_id}", result)
        self.assertEqual([row["id"] for row in remaining], [second_id])

    def test_scheduled_task_cancel_prefers_task_id_over_index(self):
        first_id = create_scheduled_task(
            "test_tooling_registry",
            "first",
            "first instruction",
            "2026-05-01T09:00:00",
            "once",
            None,
        )
        second_id = create_scheduled_task(
            "test_tooling_registry",
            "second",
            "second instruction",
            "2026-05-01T10:00:00",
            "once",
            None,
        )

        execute_tool(
            "manage_scheduled_task",
            {"action": "cancel", "task_id": second_id, "task_index": 1},
            session_id="test_tooling_registry",
        )
        remaining = list_scheduled_tasks("test_tooling_registry")

        self.assertEqual([row["id"] for row in remaining], [first_id])

    def test_scheduled_task_cancel_matching_cancels_by_query(self):
        sleep_id = create_scheduled_task(
            "test_tooling_registry",
            "睡觉提醒",
            "提醒用户睡觉",
            "2026-05-01T23:00:00",
            "once",
            None,
        )
        food_id = create_scheduled_task(
            "test_tooling_registry",
            "吃饭提醒",
            "提醒用户吃饭",
            "2026-05-01T18:00:00",
            "once",
            None,
        )

        result = execute_tool(
            "manage_scheduled_task",
            {"action": "cancel_matching", "query": "睡觉提醒"},
            session_id="test_tooling_registry",
        )
        remaining = list_scheduled_tasks("test_tooling_registry")

        self.assertIn(f"id={sleep_id}", result)
        self.assertEqual([row["id"] for row in remaining], [food_id])

    def test_scheduled_task_reschedule_matching_updates_by_query(self):
        task_id = create_scheduled_task(
            "test_tooling_registry",
            "早起提醒",
            "提醒用户起床",
            "2026-05-01T06:00:00",
            "once",
            None,
        )

        result = execute_tool(
            "manage_scheduled_task",
            {
                "action": "reschedule_matching",
                "query": "早起提醒",
                "run_at": "2026-05-01T09:00:00",
            },
            session_id="test_tooling_registry",
        )
        remaining = list_scheduled_tasks("test_tooling_registry")

        self.assertIn(f"id={task_id}", result)
        self.assertEqual(remaining[0]["run_at"], "2026-05-01T09:00:00")

    def test_scheduled_task_cancel_matching_rejects_generic_query(self):
        first_id = create_scheduled_task(
            "test_tooling_registry",
            "生日提醒",
            "提醒用户生日",
            "2026-05-01T09:00:00",
            "yearly",
            None,
        )
        second_id = create_scheduled_task(
            "test_tooling_registry",
            "喝水提醒",
            "提醒用户喝水",
            "2026-05-01T10:00:00",
            "daily",
            None,
        )

        result = execute_tool(
            "manage_scheduled_task",
            {"action": "cancel_matching", "query": "提醒"},
            session_id="test_tooling_registry",
        )
        remaining = list_scheduled_tasks("test_tooling_registry")

        self.assertIn("没有找到匹配", result)
        self.assertEqual([row["id"] for row in remaining], [first_id, second_id])

    def test_server_descriptions_expose_builtin_servers(self):
        names = {server["name"] for server in describe_tool_servers()}
        self.assertEqual(
            names,
            {"filesystem", "system", "media", "scheduler"},
        )

    def test_media_server_exposes_qwen_vision_tool(self):
        names = {tool["name"] for tool in get_chat_tool_definitions()}
        self.assertIn("mcp__media__look_at_image", names)
        self.assertIn("mcp__media__describe_image", names)

    def test_describe_image_requires_image_and_key(self):
        no_image = execute_tool(
            "mcp__media__describe_image",
            {},
            image_urls=[],
            session_id="test_tooling_registry",
        )
        self.assertIn("没有可以看的图片", no_image)

        with patch.dict(
            os.environ,
            {
                "PUPU_MEMU_EMBED_API_KEY": "",
                "PUPU_MEMU_API_KEY": "",
            },
            clear=False,
        ):
            missing_key = execute_tool(
                "mcp__media__describe_image",
                {},
                image_urls=["https://example.test/image.jpg"],
                session_id="test_tooling_registry",
            )
        self.assertIn("API Key 未配置", missing_key)

    def test_describe_image_calls_openai_compatible_vision_endpoint(self):
        os.environ["PUPU_MEMU_EMBED_API_KEY"] = "embed-key"
        os.environ["PUPU_MEMU_EMBED_BASE_URL"] = "https://dashscope.test/compatible-mode/v1"
        os.environ["PUPU_VISION_MODEL"] = "qwen3.6-flash"
        os.environ["PUPU_VISION_TIMEOUT"] = "12"

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        calls = {}

        def fake_download(url):
            calls["download_url"] = url
            return "ZmFrZS1pbWFnZQ==", "image/png"

        def fake_post(url, *, headers, json, timeout):
            calls["url"] = url
            calls["headers"] = headers
            calls["json"] = json
            calls["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": "图里有一只白色杯子。"}}]})

        with patch("pupu.tooling.servers.media.download_image_as_base64", side_effect=fake_download):
            with patch("pupu.tooling.servers.media.httpx.post", side_effect=fake_post):
                result = execute_tool(
                    "mcp__media__describe_image",
                    {"image_index": 0, "question": "图里有什么？"},
                    image_urls=["https://example.test/cup.png"],
                    session_id="test_tooling_registry",
                )

        self.assertEqual(result, "图里有一只白色杯子。")
        self.assertEqual(calls["download_url"], "https://example.test/cup.png")
        self.assertEqual(calls["url"], "https://dashscope.test/compatible-mode/v1/chat/completions")
        self.assertEqual(calls["headers"]["Authorization"], "Bearer embed-key")
        self.assertEqual(calls["timeout"], 12.0)
        self.assertEqual(calls["json"]["model"], "qwen3.6-flash")
        content = calls["json"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "图里有什么？")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_external_stdio_mcp_server_is_registered_and_callable(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
        config = [
            {
                "name": "tavily",
                "command": sys.executable,
                "args": [str(fixture)],
                "exposures": ["chat"],
                "timeout": 10,
            }
        ]
        os.environ["PUPU_MCP_SERVERS_JSON"] = json.dumps(config)
        refresh_registry()

        names = {tool["name"] for tool in tools.TOOL_DEFINITIONS}
        self.assertNotIn("mcp__tavily__tavily_search", names)
        refresh_registry()
        fresh_names = {tool["name"] for tool in get_chat_tool_definitions()}
        self.assertIn("mcp__tavily__tavily_search", fresh_names)

        result = execute_tool(
            "mcp__tavily__tavily_search",
            {"query": "火遮眼 电影"},
            session_id="test_tooling_registry",
        )

        self.assertIn("fake result for 火遮眼 电影", result)

    def test_external_stdio_mcp_server_reuses_persistent_process(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
        counter = Path(__file__).resolve().parent / "_tmp" / "fake_mcp_count.txt"
        counter.unlink(missing_ok=True)
        config = [
            {
                "name": "tavily",
                "command": sys.executable,
                "args": [str(fixture)],
                "env": {"FAKE_MCP_COUNTER_PATH": str(counter)},
                "exposures": ["chat"],
                "timeout": 10,
            }
        ]
        os.environ["PUPU_MCP_SERVERS_JSON"] = json.dumps(config)
        refresh_tool_definitions()

        execute_tool(
            "mcp__tavily__tavily_search",
            {"query": "first"},
            session_id="test_tooling_registry",
        )
        execute_tool(
            "mcp__tavily__tavily_search",
            {"query": "second"},
            session_id="test_tooling_registry",
        )

        self.assertEqual(counter.read_text(encoding="utf-8"), "1")

    def test_external_stdio_mcp_server_is_shared_across_registry_rebuilds(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
        counter = Path(__file__).resolve().parent / "_tmp" / "fake_mcp_shared_count.txt"
        counter.unlink(missing_ok=True)
        config = [
            {
                "name": "tavily",
                "command": sys.executable,
                "args": [str(fixture)],
                "env": {"FAKE_MCP_COUNTER_PATH": str(counter)},
                "exposures": ["chat"],
                "timeout": 10,
            }
        ]
        os.environ["PUPU_MCP_SERVERS_JSON"] = json.dumps(config)
        import pupu.tooling.registry as registry

        refresh_registry()
        first = registry.build_registry()
        second = registry.build_registry()

        first.execute(
            "mcp__tavily__tavily_search",
            {"query": "shared"},
        )
        second.execute(
            "mcp__tavily__tavily_search",
            {"query": "shared again"},
        )

        self.assertEqual(counter.read_text(encoding="utf-8"), "1")

    def test_external_stdio_mcp_server_restarts_after_process_exit(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
        counter = Path(__file__).resolve().parent / "_tmp" / "fake_mcp_restart_count.txt"
        counter.unlink(missing_ok=True)
        config = [
            {
                "name": "tavily",
                "command": sys.executable,
                "args": [str(fixture)],
                "env": {"FAKE_MCP_COUNTER_PATH": str(counter)},
                "exposures": ["chat"],
                "timeout": 10,
            }
        ]
        os.environ["PUPU_MCP_SERVERS_JSON"] = json.dumps(config)
        refresh_tool_definitions()

        execute_tool(
            "mcp__tavily__tavily_search",
            {"query": "__exit__"},
            session_id="test_tooling_registry",
        )
        result = execute_tool(
            "mcp__tavily__tavily_search",
            {"query": "after restart"},
            session_id="test_tooling_registry",
        )

        self.assertIn("fake result for after restart", result)
        self.assertEqual(counter.read_text(encoding="utf-8"), "2")


if __name__ == "__main__":
    unittest.main()
