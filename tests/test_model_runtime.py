import unittest
from unittest.mock import patch

from pupu.model_runtime import (
    AgentLoopLimitError,
    AgentRunError,
    AgentRunner,
    LoopPolicy,
    ModelTurn,
    ProviderCapabilities,
    ToolCall,
)


class _Adapter:
    capabilities = ProviderCapabilities(tools=True)

    def __init__(self, turn: ModelTurn) -> None:
        self.turn = turn

    def prepare_messages(self, messages, **_kwargs):
        return list(messages)

    def complete_turn(self, **_kwargs):
        return self.turn

    def append_tool_results(self, messages, _turn, _results):
        return messages

    def append_continuation(self, messages, _turn):
        return messages


class AgentRunnerTests(unittest.TestCase):
    def test_final_turn_is_rejected_after_deadline(self):
        adapter = _Adapter(ModelTurn("late", (), "stop", None))
        runner = AgentRunner(LoopPolicy(timeout_seconds=0.5))

        with patch(
            "pupu.model_runtime.time.monotonic",
            side_effect=[10.0, 10.1, 10.6],
        ):
            with self.assertRaises(AgentLoopLimitError):
                runner.run(
                    adapter,
                    model="test",
                    system="",
                    messages=[],
                    max_tokens=10,
                )

    def test_tool_result_append_failure_preserves_side_effect_state(self):
        class FailingAppendAdapter(_Adapter):
            def append_tool_results(self, messages, turn, results):
                raise TypeError("cannot serialize tool result")

        adapter = FailingAppendAdapter(
            ModelTurn(
                "",
                (ToolCall("call-1", "write", {}),),
                "tool_use",
                None,
                outcome="tool_calls",
            )
        )

        with self.assertRaises(AgentRunError) as raised:
            AgentRunner().run(
                adapter,
                model="test",
                system="",
                messages=[],
                max_tokens=10,
                tools=[{"name": "write"}],
                tool_handler=lambda *_args: "written",
            )

        self.assertTrue(raised.exception.tool_execution_started)
        self.assertEqual(raised.exception.tool_calls_executed, 1)

    def test_non_successful_terminal_outcome_is_not_returned(self):
        adapter = _Adapter(
            ModelTurn(
                "partial answer",
                (),
                "max_tokens",
                None,
                outcome="truncated",
            )
        )

        with self.assertRaises(AgentRunError) as raised:
            AgentRunner().run(
                adapter,
                model="test",
                system="",
                messages=[],
                max_tokens=10,
            )

        self.assertFalse(raised.exception.fallback_allowed)
        self.assertIn("truncated", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
