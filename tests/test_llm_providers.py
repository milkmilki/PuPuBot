import os
from pathlib import Path
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu import llm
from pupu.codex_mcp_server import _tool_definitions
from pupu.llm_providers import CodexCliProvider, _default_codex_command
from pupu.memory import init_db


class LLMProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def tearDown(self):
        llm._providers.clear()
        llm._last_provider_used.clear()

    def test_provider_routing_defaults_to_anthropic_and_accepts_codex(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUPU_CHAT_PROVIDER", None)
            self.assertEqual(llm.get_provider_name("chat"), "anthropic")

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "codex_cli"}, clear=False):
            self.assertEqual(llm.get_provider_name("chat"), "codex_cli")

    def test_last_provider_label_records_fallback_provider(self):
        class FailingProvider:
            def chat_complete(self, **kwargs):
                raise RuntimeError("boom")

        class FallbackProvider:
            def __init__(self, client):
                pass

            def chat_complete(self, **kwargs):
                return "ok"

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "codex_cli"}, clear=False):
            with patch("pupu.llm.get_provider", return_value=FailingProvider()):
                with patch("pupu.llm.AnthropicProvider", FallbackProvider):
                    with patch("pupu.llm.get_client", return_value=object()):
                        text = llm.chat_complete(
                            role="chat",
                            model="claude-test",
                            system="system",
                            messages=[{"role": "user", "content": "hi"}],
                            max_tokens=10,
                        )

        self.assertEqual(text, "ok")
        self.assertEqual(
            llm.last_provider_label("chat", "claude-test"),
            "anthropic:claude-test fallback_from=codex_cli",
        )

    def test_codex_command_includes_output_file_and_mcp_config(self):
        provider = CodexCliProvider(
            workspace_root=Path("D:/repo"),
            python_executable="D:/venv/Scripts/python.exe",
            codex_command="codex",
        )

        with patch.dict(
            os.environ,
            {
                "PUPU_CODEX_PROFILE": "pupu-fast",
                "PUPU_CODEX_REASONING_EFFORT": "low",
            },
            clear=False,
        ):
            command = provider.build_exec_command(
                output_path=Path("D:/tmp/last.txt"),
                run_dir=Path("D:/tmp/run"),
                use_mcp=True,
                session_id="owner",
                image_urls=["https://example.test/a.png"],
                is_admin=True,
                tool_exposure="chat",
            )
        joined = " ".join(command)

        self.assertIn("exec", command)
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertIn("-p", command)
        self.assertIn("pupu-fast", command)
        self.assertIn("model_reasoning_effort='low'", joined)
        self.assertIn("mcp_servers.pupu.command='D:/venv/Scripts/python.exe'", joined)
        self.assertIn("mcp_servers.pupu.env.PUPU_MCP_SESSION_ID='owner'", joined)
        self.assertIn("mcp_servers.pupu.env.PUPU_MCP_IS_ADMIN='1'", joined)
        self.assertIn("mcp_servers.pupu.env.PUPU_MCP_EXPOSURE='chat'", joined)
        self.assertEqual(command[-1], "-")

    def test_codex_command_can_use_explicit_env_path(self):
        with patch.dict(os.environ, {"PUPU_CODEX_BIN": "D:/Tools/codex.exe"}, clear=False):
            provider = CodexCliProvider(workspace_root=Path("D:/repo"))
            self.assertEqual(_default_codex_command(), "D:/Tools/codex.exe")

        self.assertEqual(provider.codex_command, "D:/Tools/codex.exe")

    def test_mcp_tool_listing_filters_admin_tools_for_non_admin(self):
        with patch.dict(
            os.environ,
            {"PUPU_MCP_EXPOSURE": "chat", "PUPU_MCP_IS_ADMIN": "0"},
            clear=False,
        ):
            non_admin_names = {tool["name"] for tool in _tool_definitions()}

        with patch.dict(
            os.environ,
            {"PUPU_MCP_EXPOSURE": "chat", "PUPU_MCP_IS_ADMIN": "1"},
            clear=False,
        ):
            admin_names = {tool["name"] for tool in _tool_definitions()}

        self.assertIn("mcp__web__search", non_admin_names)
        self.assertNotIn("mcp__system__run_command", non_admin_names)
        self.assertIn("mcp__system__run_command", admin_names)

    def test_codex_tool_bridge_executes_local_tool_handler(self):
        provider = CodexCliProvider(workspace_root=Path("D:/repo"))
        calls = []
        replies = iter(
            [
                '{"type":"tool_call","name":"mcp__web__search","input":{"query":"pupu"},"reason":"need search"}',
                '{"type":"final","text":"done"}',
            ]
        )

        def fake_run(*args, **kwargs):
            return next(replies)

        def fake_tool(name, tool_input, reason):
            calls.append((name, tool_input, reason))
            return "search result"

        with patch.object(CodexCliProvider, "_run_codex_exec", fake_run):
            text = provider.chat_complete(
                system="system",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=100,
                tools=[
                    {
                        "name": "mcp__web__search",
                        "description": "search",
                        "input_schema": {"type": "object"},
                    }
                ],
                tool_handler=fake_tool,
            )

        self.assertEqual(text, "done")
        self.assertEqual(calls[0][0], "mcp__web__search")
        self.assertEqual(calls[0][1], {"query": "pupu"})


if __name__ == "__main__":
    unittest.main()
