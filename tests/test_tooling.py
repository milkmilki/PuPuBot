import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.memory import (
    cancel_scheduled_task,
    create_scheduled_task,
    init_db,
    list_scheduled_tasks,
    reset_session,
)
from pupu.tools import (
    PROACTIVE_TOOL_DEFINITIONS,
    TOOL_DEFINITIONS,
    describe_tool_servers,
    execute_tool,
    is_admin_tool,
)


class ToolingRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        reset_session("test_tooling_registry")

    def test_chat_tools_are_namespaced(self):
        names = {tool["name"] for tool in TOOL_DEFINITIONS}
        self.assertIn("mcp__web__search", names)
        self.assertIn("mcp__scheduler__manage_scheduled_task", names)
        self.assertNotIn("web_search", names)

    def test_admin_flags_work_for_canonical_and_legacy_names(self):
        self.assertTrue(is_admin_tool("read_file"))
        self.assertTrue(is_admin_tool("mcp__filesystem__read_file"))
        self.assertTrue(is_admin_tool("mcp__system__run_command"))
        self.assertFalse(is_admin_tool("mcp__web__search"))

    def test_proactive_tools_are_filtered(self):
        names = {tool["name"] for tool in PROACTIVE_TOOL_DEFINITIONS}
        self.assertEqual(
            names,
            {
                "mcp__web__search",
                "mcp__web__fetch_url",
            },
        )

    def test_legacy_dispatch_still_works(self):
        result = execute_tool(
            "manage_scheduled_task",
            {"action": "list"},
            session_id="test_tooling_registry",
        )
        self.assertIn("当前没有待执行的定时任务", result)
        self.assertIn("总结进度：0/8，还差 8 轮触发自动总结", result)

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
            {"web", "filesystem", "system", "media", "scheduler"},
        )


if __name__ == "__main__":
    unittest.main()
