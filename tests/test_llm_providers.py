import os
from pathlib import Path
import unittest
from tests.helpers import activate_test_instance
from unittest.mock import Mock, patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu import llm
from pupu.llm_providers import AnthropicProvider, OpenAICompatibleProvider
from pupu.memory import init_db


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


if __name__ == "__main__":
    unittest.main()
