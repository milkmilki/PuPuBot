import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.memory import init_db, reset_session
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

    def test_server_descriptions_expose_builtin_servers(self):
        names = {server["name"] for server in describe_tool_servers()}
        self.assertEqual(
            names,
            {"web", "filesystem", "system", "media", "scheduler"},
        )


if __name__ == "__main__":
    unittest.main()
