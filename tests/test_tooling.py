import io
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import MCPError

from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

import pupu.tools as tools
from pupu.agent import REVIEW_INTERVAL
from pupu.memory import (
    cancel_scheduled_task,
    create_scheduled_task,
    init_db,
    list_scheduled_tasks,
    reset_session,
)
from pupu.model_runtime import ToolResult
from pupu.tooling import refresh_registry
from pupu.tooling.base import BuiltinToolServer, ToolSpec
from pupu.tooling.external_mcp import (
    ExternalMcpToolServer,
    PersistentMcpStdioSession,
    _BoundedTextLog,
    _tool_result_from_mcp,
    build_external_mcp_servers,
)
from pupu.tooling.registry import ToolRegistry
from pupu.tools import (
    describe_tool_servers,
    execute_tool,
    get_chat_tool_definitions,
    is_admin_tool,
    refresh_tool_definitions,
)


class ToolingRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        reset_session("test_tooling_registry")
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
        refresh_tool_definitions()

    def tearDown(self):
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
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
            {"filesystem", "system", "scheduler"},
        )

    def test_removed_media_server_is_not_exposed(self):
        names = {tool["name"] for tool in get_chat_tool_definitions()}
        self.assertNotIn("mcp__media__describe_image", names)
        self.assertNotIn("mcp__media__look_at_image", names)

    def test_external_mcp_load_error_redacts_env_secret(self):
        os.environ["PUPU_MCP_SERVERS_JSON"] = json.dumps(
            [
                {
                    "name": "bad",
                    "command": "fake-command",
                    "env": {"BAD_API_KEY": "super-secret-token"},
                }
            ]
        )

        class FakeExternalServer:
            def __init__(self, config):
                self.config = config

            def list_tools(self):
                raise RuntimeError(f"loader echoed {self.config['env']['BAD_API_KEY']}")

            def close(self):
                pass

        captured = io.StringIO()
        with patch("pupu.tooling.external_mcp.ExternalMcpToolServer", FakeExternalServer):
            with patch("sys.stdout", captured):
                servers = build_external_mcp_servers()

        output = captured.getvalue()
        self.assertEqual(servers, [])
        self.assertIn("[secret]", output)
        self.assertNotIn("super-secret-token", output)

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

    def test_external_mcp_uses_modern_protocol_when_server_supports_it(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "fake_modern_mcp_server.py"
        server = ExternalMcpToolServer(
            {
                "name": "modern",
                "command": sys.executable,
                "args": [str(fixture)],
                "timeout": 10,
            }
        )
        self.addCleanup(server.close)

        tools = server.list_tools()
        result = tools[0].handler({"query": "hello"}, None)

        self.assertEqual(server._session.protocol_version, "2026-07-28")
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(result.content[0]["text"], "modern result for hello")
        self.assertEqual(result.structured_content, {"result": "modern result for hello"})

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

    def test_concurrent_session_start_reuses_starting_worker(self):
        class SlowStartingSession(PersistentMcpStdioSession):
            def __init__(self):
                super().__init__(
                    name="slow",
                    command="unused",
                    args=[],
                    env={},
                    cwd=None,
                    timeout=1,
                )
                self.start_count = 0
                self.release = threading.Event()

            def _thread_main(self, startup, _commands):
                self.start_count += 1
                time.sleep(0.05)
                with self._state_changed:
                    self._state = type(self._state).RUNNING
                    startup.ready.set()
                    self._state_changed.notify_all()
                self.release.wait(timeout=1)

        session = SlowStartingSession()
        self.addCleanup(session.close)
        barrier = threading.Barrier(3)
        errors = []

        def start():
            try:
                barrier.wait()
                session.ensure_started()
            except Exception as exc:
                errors.append(exc)

        callers = [threading.Thread(target=start) for _ in range(2)]
        for caller in callers:
            caller.start()
        barrier.wait()
        for caller in callers:
            caller.join(timeout=1)

        self.assertEqual(errors, [])
        self.assertEqual(session.start_count, 1)
        session.release.set()

    def test_mcp_stderr_log_retains_only_bounded_tail(self):
        stderr = _BoundedTextLog(max_bytes=1024)
        self.addCleanup(stderr.close)

        stderr.write("prefix-" + ("x" * 4096) + "-tail\n")
        stderr.flush()
        deadline = time.monotonic() + 1
        tail = ""
        while time.monotonic() < deadline:
            tail = stderr.tail()
            if tail.endswith("-tail"):
                break
            time.sleep(0.01)

        self.assertLessEqual(len(tail.encode("utf-8")), 1024)
        self.assertTrue(tail.endswith("-tail"))

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
        with self.assertRaises(MCPError):
            execute_tool(
                "mcp__tavily__tavily_search",
                {"query": "after restart"},
                session_id="test_tooling_registry",
            )
        result = execute_tool(
            "mcp__tavily__tavily_search",
            {"query": "after restart"},
            session_id="test_tooling_registry",
        )

        self.assertIn("fake result for after restart", result)
        self.assertEqual(counter.read_text(encoding="utf-8"), "2")

    def test_external_stdio_close_closes_stdin_after_process_exit(self):
        session = PersistentMcpStdioSession(
            name="fake",
            command=sys.executable,
            args=[],
            env={},
            cwd=None,
            timeout=1,
        )

        class ExitedProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()

            def poll(self) -> int:
                return 0

            def terminate(self) -> None:
                raise AssertionError("already exited process should not be terminated")

            def wait(self, timeout=None) -> int:
                return 0

            def kill(self) -> None:
                raise AssertionError("already exited process should not be killed")

        proc = ExitedProcess()
        session._proc = proc  # type: ignore[assignment]

        session.close()

        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_mcp_error_and_structured_content_are_preserved(self):
        result = _tool_result_from_mcp(
            {
                "content": [{"type": "text", "text": "invalid query"}],
                "structuredContent": {"field": "query"},
                "isError": True,
            }
        )

        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content, {"field": "query"})

    def test_duplicate_raw_tool_aliases_keep_qualified_names(self):
        def server(name: str):
            return BuiltinToolServer(
                name=name,
                description=name,
                tools=(
                    ToolSpec(
                        server=name,
                        name="search",
                        description="search",
                        input_schema={"type": "object"},
                        handler=lambda _input, _context: name,
                    ),
                ),
            )

        registry = ToolRegistry([server("one"), server("two")])

        self.assertEqual(registry.execute("mcp__one__search", {}), "one")
        self.assertEqual(registry.execute("mcp__two__search", {}), "two")
        with self.assertRaises(KeyError):
            registry.resolve("search")


if __name__ == "__main__":
    unittest.main()
