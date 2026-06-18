import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu import llm
from pupu.codex_mcp_server import _tool_definitions
from pupu.llm_providers import (
    AnthropicProvider,
    CodexCliProvider,
    OpenAICompatibleProvider,
    _codex_subprocess_env,
    _default_codex_command,
)
from pupu.memory import init_db
from pupu.sessions import OWNER_SESSION


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

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "xiaoshuoai"}, clear=False):
            self.assertEqual(llm.get_provider_name("chat"), "xiaoshuoai")

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "deepseek"}, clear=False):
            self.assertEqual(llm.get_provider_name("chat"), "deepseek")

    def test_set_provider_name_updates_runtime_environment(self):
        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "anthropic"}, clear=False):
            llm.set_provider_name("chat", "xiaoshuoai")

            self.assertEqual(os.environ["PUPU_CHAT_PROVIDER"], "xiaoshuoai")
            self.assertEqual(llm.get_provider_name("chat"), "xiaoshuoai")

    def test_judge_temperature_defaults_low_and_can_be_overridden(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUPU_JUDGE_TEMPERATURE", None)
            self.assertEqual(llm.role_temperature("judge"), 0.1)
            self.assertIsNone(llm.role_temperature("chat"))

        with patch.dict(os.environ, {"PUPU_JUDGE_TEMPERATURE": "0.25"}, clear=False):
            self.assertEqual(llm.role_temperature("judge"), 0.25)

        with patch.dict(os.environ, {"PUPU_JUDGE_TEMPERATURE": "bad"}, clear=False):
            self.assertEqual(llm.role_temperature("judge"), 0.1)

    def test_xiaoshuoai_provider_is_created_from_env(self):
        with patch.dict(
            os.environ,
            {
                "PUPU_CHAT_PROVIDER": "xiaoshuoai",
                "PUPU_XIAOSHUOAI_BASE_URL": "https://example.test/chat/completions",
                "PUPU_XIAOSHUOAI_API_KEY": "test-key",
                "PUPU_XIAOSHUOAI_MODEL": "novel-model",
                "PUPU_XIAOSHUOAI_TIMEOUT": "12.5",
                "PUPU_XIAOSHUOAI_TEMPERATURE": "0.6",
            },
            clear=False,
        ):
            provider = llm.get_provider("chat")

        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertEqual(provider.endpoint, "https://example.test/chat/completions")
        self.assertEqual(provider.model, "novel-model")
        self.assertEqual(provider.timeout_seconds, 12.5)
        self.assertEqual(provider.temperature, 0.6)

    def test_deepseek_provider_is_created_from_env(self):
        fake_client = Mock()
        text_block = Mock()
        text_block.type = "text"
        text_block.text = "ok"
        fake_client.messages.create.return_value = Mock(stop_reason="end_turn", content=[text_block])
        with patch("pupu.llm.anthropic.Anthropic", return_value=fake_client) as ctor:
            with patch.dict(
                os.environ,
                {
                    "PUPU_CHAT_PROVIDER": "deepseek",
                    "PUPU_DEEPSEEK_BASE_URL": "https://api.deepseek.com/anthropic",
                    "PUPU_DEEPSEEK_API_KEY": "test-key",
                    "PUPU_DEEPSEEK_MODEL": "deepseek-v4-pro",
                },
                clear=False,
            ):
                provider = llm.get_provider("chat")

        result = provider.chat_complete(
            model="claude-opus-4-6",
            system="system",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=20,
        )

        self.assertEqual(result, "ok")
        ctor.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.deepseek.com/anthropic",
        )
        self.assertEqual(fake_client.messages.create.call_args.kwargs["model"], "deepseek-v4-pro")

    def test_deepseek_provider_accepts_temperature_argument(self):
        fake_client = Mock()
        text_block = Mock()
        text_block.type = "text"
        text_block.text = "ok"
        fake_client.messages.create.return_value = Mock(stop_reason="end_turn", content=[text_block])
        with patch("pupu.llm.anthropic.Anthropic", return_value=fake_client):
            with patch.dict(
                os.environ,
                {
                    "PUPU_CHAT_PROVIDER": "deepseek",
                    "PUPU_DEEPSEEK_API_KEY": "test-key",
                    "PUPU_DEEPSEEK_MODEL": "deepseek-v4-pro",
                },
                clear=False,
            ):
                provider = llm.get_provider("chat")

        result = provider.chat_complete(
            model="claude-opus-4-6",
            system="system",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=20,
            temperature=0.2,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(fake_client.messages.create.call_args.kwargs["temperature"], 0.2)

    def test_deepseek_provider_supports_tool_calls_via_anthropic_loop(self):
        tool_block = Mock()
        tool_block.type = "tool_use"
        tool_block.name = "get_date"
        tool_block.input = {}
        tool_block.id = "tool-1"

        text_block = Mock()
        text_block.type = "text"
        text_block.text = "工具调用完成"

        first_response = Mock()
        first_response.stop_reason = "tool_use"
        first_response.content = [tool_block]

        second_response = Mock()
        second_response.stop_reason = "end_turn"
        second_response.content = [text_block]

        fake_client = Mock()
        fake_client.messages.create.side_effect = [first_response, second_response]

        with patch("pupu.llm.anthropic.Anthropic", return_value=fake_client):
            with patch.dict(
                os.environ,
                {
                    "PUPU_CHAT_PROVIDER": "deepseek",
                    "PUPU_DEEPSEEK_BASE_URL": "https://api.deepseek.com/anthropic",
                    "PUPU_DEEPSEEK_API_KEY": "test-key",
                    "PUPU_DEEPSEEK_MODEL": "deepseek-v4-pro",
                },
                clear=False,
            ):
                provider = llm.get_provider("chat")

        result = provider.chat_complete(
            model="deepseek-v4-pro",
            system="system",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=20,
            tools=[{"name": "get_date"}],
            tool_handler=lambda name, payload, hint: "2026-05-01",
        )

        self.assertEqual(result, "工具调用完成")
        self.assertEqual(fake_client.messages.create.call_count, 2)
        self.assertEqual(fake_client.messages.create.call_args_list[0].kwargs["model"], "deepseek-v4-pro")

    def test_deepseek_provider_applies_effort_from_env(self):
        fake_client = Mock()
        text_block = Mock()
        text_block.type = "text"
        text_block.text = "ok"
        fake_client.messages.create.return_value = Mock(stop_reason="end_turn", content=[text_block])

        with patch("pupu.llm.anthropic.Anthropic", return_value=fake_client):
            with patch.dict(
                os.environ,
                {
                    "PUPU_CHAT_PROVIDER": "deepseek",
                    "PUPU_DEEPSEEK_BASE_URL": "https://api.deepseek.com/anthropic",
                    "PUPU_DEEPSEEK_API_KEY": "test-key",
                    "PUPU_DEEPSEEK_MODEL": "deepseek-v4-pro",
                    "PUPU_DEEPSEEK_EFFORT": "xhigh",
                },
                clear=False,
            ):
                provider = llm.get_provider("chat")

        result = provider.chat_complete(
            model="deepseek-v4-pro",
            system="system",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=20,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(
            fake_client.messages.create.call_args.kwargs["output_config"],
            {"effort": "max"},
        )

    def test_judge_json_task_passes_low_temperature_to_provider(self):
        class FakeProvider:
            def json_task(self, **kwargs):
                self.kwargs = kwargs
                return "{}"

        provider = FakeProvider()
        with patch.dict(os.environ, {"PUPU_JUDGE_TEMPERATURE": "0.12"}, clear=False):
            with patch("pupu.llm.get_provider", return_value=provider):
                text = llm.json_task(
                    role="judge",
                    model="judge-model",
                    system="system",
                    user_content="input",
                    max_tokens=20,
                    task_name="batch_review",
                )

        self.assertEqual(text, "{}")
        self.assertEqual(provider.kwargs["temperature"], 0.12)

    def test_openai_compatible_provider_posts_chat_completion(self):
        provider = OpenAICompatibleProvider(
            name="xiaoshuoai",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            model="novel-model",
            timeout_seconds=12,
            temperature=0.6,
        )
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "你好呀"}}]
        }

        with patch("pupu.llm_providers.httpx.post", return_value=fake_response) as post:
            text = provider.chat_complete(
                model="ignored-model",
                system="system",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=20,
            )

        self.assertEqual(text, "你好呀")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["model"], "novel-model")
        self.assertEqual(kwargs["json"]["temperature"], 0.6)
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(
            kwargs["json"]["messages"],
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hi"},
            ],
        )

    def test_openai_compatible_provider_allows_per_call_temperature(self):
        provider = OpenAICompatibleProvider(
            name="xiaoshuoai",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            model="novel-model",
            temperature=0.7,
        )
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}

        with patch("pupu.llm_providers.httpx.post", return_value=fake_response) as post:
            provider.json_task(
                model="ignored-model",
                system="system",
                user_content="input",
                max_tokens=20,
                temperature=0.1,
            )

        self.assertEqual(post.call_args.kwargs["json"]["temperature"], 0.1)

    def test_openai_compatible_provider_accepts_sse_response_text(self):
        provider = OpenAICompatibleProvider(
            name="xiaoshuoai",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            model="novel-model",
        )
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.side_effect = ValueError("not json")
        fake_response.text = "\n".join(
            [
                'data: {"choices":[{"delta":{"content":"停"}}]}',
                'data: {"choices":[{"delta":{"content":"\\n\\n"}}]}',
                'data: {"choices":[{"delta":{"content":"姐姐知道。"}}]}',
                "data: [DONE]",
            ]
        )

        with patch("pupu.llm_providers.httpx.post", return_value=fake_response):
            text = provider.chat_complete(
                model="ignored-model",
                system="system",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=20,
            )

        self.assertEqual(text, "停\n\n姐姐知道。")

    def test_openai_compatible_provider_can_enable_thinking_mode(self):
        provider = OpenAICompatibleProvider(
            name="deepseek",
            endpoint="https://api.deepseek.com/chat/completions",
            api_key="test-key",
            model="deepseek-v4-pro",
            reasoning_effort="max",
            thinking_enabled=True,
        )
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "你好呀"}}]
        }

        with patch("pupu.llm_providers.httpx.post", return_value=fake_response) as post:
            text = provider.chat_complete(
                model="ignored-model",
                system="system",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=20,
            )

        self.assertEqual(text, "你好呀")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["reasoning_effort"], "high")
        self.assertEqual(
            kwargs["json"]["extra_body"],
            {"thinking": {"type": "enabled"}},
        )

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
                session_id=OWNER_SESSION,
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

    def test_codex_command_prefers_real_candidate_before_path_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "Roaming"
            npm = appdata / "npm"
            npm.mkdir(parents=True)
            codex_cmd = npm / "codex.cmd"
            codex_cmd.write_text("@echo off\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"APPDATA": str(appdata), "PUPU_CODEX_BIN": ""},
                clear=False,
            ):
                with patch(
                    "pupu.llm_providers.shutil.which",
                    return_value="C:/Program Files/WindowsApps/OpenAI.Codex/app/resources/codex.exe",
                ):
                    self.assertEqual(_default_codex_command(), str(codex_cmd))

    def test_codex_subprocess_env_can_inject_proxy(self):
        with patch.dict(
            os.environ,
            {
                "PUPU_CODEX_PROXY": "http://127.0.0.1:7890",
                "PUPU_CODEX_NO_PROXY": "localhost,127.0.0.1",
            },
            clear=False,
        ):
            env = _codex_subprocess_env()

        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["ALL_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")

    def test_codex_subprocess_env_is_none_without_proxy(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUPU_CODEX_PROXY", None)
            self.assertIsNone(_codex_subprocess_env())

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

        self.assertIn("mcp__scheduler__manage_scheduled_task", non_admin_names)
        self.assertNotIn("mcp__system__run_command", non_admin_names)
        self.assertIn("mcp__system__run_command", admin_names)

    def test_codex_tool_bridge_executes_local_tool_handler(self):
        provider = CodexCliProvider(workspace_root=Path("D:/repo"))
        calls = []
        replies = iter(
            [
                '{"type":"tool_call","name":"mcp__scheduler__manage_scheduled_task","input":{"action":"list"},"reason":"need list"}',
                '{"type":"final","text":"done"}',
            ]
        )

        def fake_run(*args, **kwargs):
            return next(replies)

        def fake_tool(name, tool_input, reason):
            calls.append((name, tool_input, reason))
            return "task list"

        with (
            patch.dict(os.environ, {"PUPU_CODEX_TOOL_MODE": "bridge"}, clear=False),
            patch.object(CodexCliProvider, "_run_codex_exec", fake_run),
        ):
            text = provider.chat_complete(
                system="system",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=100,
                tools=[
                    {
                        "name": "mcp__scheduler__manage_scheduled_task",
                        "description": "manage tasks",
                        "input_schema": {"type": "object"},
                    }
                ],
                tool_handler=fake_tool,
            )

        self.assertEqual(text, "done")
        self.assertEqual(calls[0][0], "mcp__scheduler__manage_scheduled_task")
        self.assertEqual(calls[0][1], {"action": "list"})

    def test_codex_cli_uses_real_mcp_by_default_for_tools(self):
        provider = CodexCliProvider(workspace_root=Path("D:/repo"))
        captured = {}
        tool_calls = []

        def fake_run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return "final reply"

        def fake_tool(name, tool_input, reason):
            tool_calls.append((name, tool_input, reason))
            return "should not run locally"

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(CodexCliProvider, "_run_codex_exec", fake_run),
        ):
            os.environ.pop("PUPU_CODEX_TOOL_MODE", None)
            text = provider.chat_complete(
                system="system",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=100,
                tools=[
                    {
                        "name": "mcp__scheduler__manage_scheduled_task",
                        "description": "manage tasks",
                        "input_schema": {"type": "object"},
                    }
                ],
                tool_handler=fake_tool,
                session_id=OWNER_SESSION,
                is_admin=True,
            )

        self.assertEqual(text, "final reply")
        self.assertEqual(tool_calls, [])
        self.assertTrue(captured["use_mcp"])
        self.assertEqual(captured["session_id"], OWNER_SESSION)
        self.assertTrue(captured["is_admin"])
        self.assertIn("不要用文字、JSON 或代码块模拟工具调用", captured["prompt"])
        self.assertIn("不要输出 JSON", captured["prompt"])

    def test_codex_command_includes_external_mcp_servers_from_env(self):
        provider = CodexCliProvider(
            workspace_root=Path("D:/repo"),
            python_executable="D:/venv/Scripts/python.exe",
            codex_command="codex",
        )
        external = [
            {
                "name": "brave-search",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                "env": {"BRAVE_API_KEY": "test-key"},
                "cwd": "D:/repo",
            },
            {
                "name": "pupu",
                "command": "ignored",
            },
        ]

        with patch.dict(
            os.environ,
            {"PUPU_CODEX_MCP_SERVERS_JSON": json.dumps(external)},
            clear=False,
        ):
            command = provider.build_exec_command(
                output_path=Path("D:/tmp/last.txt"),
                run_dir=Path("D:/tmp/run"),
                use_mcp=True,
                session_id=OWNER_SESSION,
                image_urls=[],
                is_admin=False,
                tool_exposure="chat",
            )
        joined = " ".join(command)

        self.assertIn("mcp_servers.pupu.command='D:/venv/Scripts/python.exe'", joined)
        self.assertIn("mcp_servers.brave-search.command='npx'", joined)
        self.assertIn(
            "mcp_servers.brave-search.args=['-y','@modelcontextprotocol/server-brave-search']",
            joined,
        )
        self.assertIn("mcp_servers.brave-search.env.BRAVE_API_KEY='test-key'", joined)
        self.assertIn("mcp_servers.brave-search.cwd='D:/repo'", joined)


if __name__ == "__main__":
    unittest.main()
