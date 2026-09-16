import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu import llm
from pupu.llm_providers import AnthropicProvider, OpenAICompatibleProvider
from pupu.memory import init_db
from pupu.model_runtime import AgentLoopLimitError, AgentRunError


class LLMProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def tearDown(self):
        llm._providers.clear()
        llm._last_provider_used.clear()

    def test_provider_routing_defaults_to_anthropic_and_accepts_api_providers(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUPU_CHAT_PROVIDER", None)
            self.assertEqual(llm.get_provider_name("chat"), "anthropic")

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "xiaoshuoai"}, clear=False):
            self.assertEqual(llm.get_provider_name("chat"), "xiaoshuoai")

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "deepseek"}, clear=False):
            self.assertEqual(llm.get_provider_name("chat"), "deepseek")

    def test_codex_cli_is_not_a_supported_provider(self):
        with self.assertRaises(llm.ProviderError):
            llm.set_provider_name("chat", "codex_cli")

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "codex_cli"}, clear=False):
            with self.assertRaises(llm.ProviderError):
                llm.get_provider("chat")
            with self.assertRaises(llm.ProviderError):
                llm.chat_complete(
                    role="chat",
                    model="ignored",
                    system="system",
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=10,
                )
            with self.assertRaises(llm.ProviderConfigError):
                llm.validate_model_provider_config(roles=("chat",))

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
            max_retries=0,
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

    def test_deepseek_provider_sends_images_as_native_anthropic_blocks(self):
        fake_client = Mock()
        text_block = Mock()
        text_block.type = "text"
        text_block.text = "红色"
        fake_client.messages.create.return_value = Mock(stop_reason="end_turn", content=[text_block])
        with patch("pupu.llm.anthropic.Anthropic", return_value=fake_client):
            with patch.dict(
                os.environ,
                {
                    "PUPU_CHAT_PROVIDER": "deepseek",
                    "PUPU_DEEPSEEK_API_KEY": "test-key",
                    "PUPU_DEEPSEEK_MODEL": "deepseek-v4-flash-vision-exp",
                },
                clear=False,
            ):
                provider = llm.get_provider("chat")

        with patch(
            "pupu.llm_providers.download_image_as_base64",
            return_value=("cG5nLWJ5dGVz", "image/png"),
        ):
            result = provider.chat_complete(
                model="ignored",
                system="system",
                messages=[{"role": "user", "content": "这是什么颜色？"}],
                max_tokens=20,
                image_urls=["https://example.test/red.png"],
                session_id="owner",
            )

        self.assertEqual(result, "红色")
        request_messages = fake_client.messages.create.call_args.kwargs["messages"]
        content = request_messages[-1]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["type"], "base64")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[0]["source"]["data"], "cG5nLWJ5dGVz")
        self.assertEqual(content[1], {"type": "text", "text": "这是什么颜色？"})

    def test_deepseek_provider_supports_tool_calls_via_anthropic_loop(self):
        tool_block = Mock()
        tool_block.type = "tool_use"
        tool_block.name = "get_date"
        tool_block.input = {}
        tool_block.id = "tool-1"

        text_block = Mock()
        text_block.type = "text"
        text_block.text = "tool done"

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

        self.assertEqual(result, "tool done")
        self.assertEqual(fake_client.messages.create.call_count, 2)
        self.assertEqual(fake_client.messages.create.call_args_list[0].kwargs["model"], "deepseek-v4-pro")

    def test_anthropic_tool_error_is_returned_to_model(self):
        tool_block = Mock(type="tool_use", name="fail", input={}, id="tool-1")
        text_block = Mock(type="text", text="recovered")
        fake_client = Mock()
        fake_client.messages.create.side_effect = [
            Mock(stop_reason="tool_use", content=[tool_block]),
            Mock(stop_reason="end_turn", content=[text_block]),
        ]
        provider = AnthropicProvider(fake_client)

        result = provider.chat_complete(
            model="claude-test",
            system="system",
            messages=[{"role": "user", "content": "run"}],
            max_tokens=20,
            tools=[{"name": "fail"}],
            tool_handler=lambda *_: (_ for _ in ()).throw(RuntimeError("broken")),
        )

        self.assertEqual(result, "recovered")
        tool_result = fake_client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("RuntimeError: broken", tool_result["content"])

    def test_anthropic_tool_loop_enforces_round_limit(self):
        tool_block = Mock(type="tool_use", name="again", input={}, id="tool-1")
        fake_client = Mock()
        fake_client.messages.create.return_value = Mock(
            stop_reason="tool_use",
            content=[tool_block],
        )
        provider = AnthropicProvider(fake_client)

        with patch.dict(os.environ, {"PUPU_TOOL_MAX_ROUNDS": "1"}, clear=False):
            with self.assertRaises(AgentLoopLimitError):
                provider.chat_complete(
                    model="claude-test",
                    system="system",
                    messages=[{"role": "user", "content": "loop"}],
                    max_tokens=20,
                    tools=[{"name": "again"}],
                    tool_handler=lambda *_: "again",
                )

        self.assertEqual(fake_client.messages.create.call_count, 2)

    def test_anthropic_truncated_turn_is_not_returned_as_complete(self):
        fake_client = Mock()
        fake_client.messages.create.return_value = Mock(
            stop_reason="max_tokens",
            content=[Mock(type="text", text="partial")],
        )

        with self.assertRaises(AgentRunError) as raised:
            AnthropicProvider(fake_client).chat_complete(
                model="claude-test",
                system="system",
                messages=[{"role": "user", "content": "long answer"}],
                max_tokens=20,
            )

        self.assertFalse(raised.exception.fallback_allowed)
        self.assertIn("truncated", str(raised.exception))

    def test_tool_call_budget_is_checked_before_parallel_calls_execute(self):
        first = Mock(type="tool_use", name="first", input={}, id="tool-1")
        second = Mock(type="tool_use", name="second", input={}, id="tool-2")
        fake_client = Mock()
        fake_client.messages.create.return_value = Mock(
            stop_reason="tool_use",
            content=[first, second],
        )
        provider = AnthropicProvider(fake_client)
        executed = []

        with patch.dict(os.environ, {"PUPU_TOOL_MAX_CALLS": "1"}, clear=False):
            with self.assertRaises(AgentLoopLimitError):
                provider.chat_complete(
                    model="claude-test",
                    system="system",
                    messages=[{"role": "user", "content": "run both"}],
                    max_tokens=20,
                    tools=[{"name": "first"}, {"name": "second"}],
                    tool_handler=lambda name, *_: executed.append(name) or "ok",
                )

        self.assertEqual(executed, [])

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
            "choices": [{"message": {"content": "hello"}}]
        }

        with patch("pupu.llm_providers.httpx.post", return_value=fake_response) as post:
            text = provider.chat_complete(
                model="ignored-model",
                system="system",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=20,
            )

        self.assertEqual(text, "hello")
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

    def test_openai_compatible_provider_executes_tool_loop(self):
        provider = OpenAICompatibleProvider(
            name="xiaoshuoai",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            model="novel-model",
        )
        first_response = Mock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "get_date",
                                    "arguments": '{"timezone":"Asia/Shanghai"}',
                                },
                            }
                        ],
                    },
                }
            ]
        }
        second_response = Mock()
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "今天是 2026-08-24"},
                }
            ]
        }

        with patch(
            "pupu.llm_providers.httpx.post",
            side_effect=[first_response, second_response],
        ) as post:
            result = provider.chat_complete(
                model="ignored",
                system="system",
                messages=[{"role": "user", "content": "今天几号？"}],
                max_tokens=20,
                tools=[
                    {
                        "name": "get_date",
                        "description": "Get date",
                        "input_schema": {
                            "type": "object",
                            "properties": {"timezone": {"type": "string"}},
                        },
                    }
                ],
                tool_handler=lambda name, arguments, _hint: (
                    f"{name}:{arguments['timezone']}:2026-08-24"
                ),
            )

        self.assertEqual(result, "今天是 2026-08-24")
        first_payload = post.call_args_list[0].kwargs["json"]
        self.assertEqual(first_payload["tools"][0]["type"], "function")
        self.assertEqual(first_payload["tools"][0]["function"]["name"], "get_date")
        second_messages = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual(second_messages[-2]["role"], "assistant")
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertEqual(second_messages[-1]["tool_call_id"], "call-1")

    def test_openai_filtered_turn_is_not_returned_as_complete(self):
        provider = OpenAICompatibleProvider(
            name="xiaoshuoai",
            endpoint="https://example.test/chat/completions",
            api_key="test-key",
            model="novel-model",
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "finish_reason": "content_filter",
                    "message": {"role": "assistant", "content": "partial"},
                }
            ]
        }

        with patch("pupu.llm_providers.httpx.post", return_value=response):
            with self.assertRaises(AgentRunError) as raised:
                provider.chat_complete(
                    model="ignored",
                    system="system",
                    messages=[{"role": "user", "content": "answer"}],
                    max_tokens=20,
                )

        self.assertFalse(raised.exception.fallback_allowed)
        self.assertIn("refused", str(raised.exception))

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
                'data: {"choices":[{"delta":{"content":"hello"}}]}',
                'data: {"choices":[{"delta":{"content":"\\n"}}]}',
                'data: {"choices":[{"delta":{"content":"there"}}]}',
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

        self.assertEqual(text, "hello\nthere")

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
            "choices": [{"message": {"content": "hello"}}]
        }

        with patch("pupu.llm_providers.httpx.post", return_value=fake_response) as post:
            text = provider.chat_complete(
                model="ignored-model",
                system="system",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=20,
            )

        self.assertEqual(text, "hello")
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

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "deepseek"}, clear=False):
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
            "anthropic:claude-test fallback_from=deepseek",
        )

    def test_provider_failure_after_tool_execution_does_not_fallback(self):
        class FailingProvider:
            def chat_complete(self, **kwargs):
                raise AgentRunError(
                    "follow-up failed",
                    tool_execution_started=True,
                    model_rounds=2,
                    tool_calls_executed=1,
                )

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "deepseek"}, clear=False):
            with patch("pupu.llm.get_provider", return_value=FailingProvider()):
                with patch("pupu.llm.AnthropicProvider") as fallback:
                    with self.assertRaises(AgentRunError):
                        llm.chat_complete(
                            role="chat",
                            model="claude-test",
                            system="system",
                            messages=[{"role": "user", "content": "hi"}],
                            max_tokens=10,
                        )

        fallback.assert_not_called()

    def test_non_retryable_agent_failure_does_not_fallback(self):
        class FailingProvider:
            def chat_complete(self, **kwargs):
                raise AgentRunError(
                    "content filtered",
                    tool_execution_started=False,
                    model_rounds=1,
                    tool_calls_executed=0,
                    fallback_allowed=False,
                )

        with patch.dict(os.environ, {"PUPU_CHAT_PROVIDER": "deepseek"}, clear=False):
            with patch("pupu.llm.get_provider", return_value=FailingProvider()):
                with patch("pupu.llm.AnthropicProvider") as fallback:
                    with self.assertRaises(AgentRunError):
                        llm.chat_complete(
                            role="chat",
                            model="claude-test",
                            system="system",
                            messages=[{"role": "user", "content": "hi"}],
                            max_tokens=10,
                        )

        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
