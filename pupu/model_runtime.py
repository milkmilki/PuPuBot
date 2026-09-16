"""Provider-neutral model events and the shared tool execution loop."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ToolContent = str | list[dict[str, Any]]
TurnOutcome = Literal[
    "completed",
    "tool_calls",
    "continuation",
    "truncated",
    "refused",
    "failed",
]

_CURRENT_AGENT_DEADLINE: ContextVar[float | None] = ContextVar(
    "pupu_agent_deadline",
    default=None,
)


@dataclass(slots=True, frozen=True)
class ProviderCapabilities:
    tools: bool = False
    vision: bool = False
    parallel_tool_calls: bool = False
    continuation: bool = False


@dataclass(slots=True, frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str = ""


@dataclass(slots=True, frozen=True)
class ToolResult:
    content: ToolContent
    is_error: bool = False
    structured_content: Any = None


ToolHandler = Callable[[str, dict[str, Any], str | None], ToolContent | ToolResult]


@dataclass(slots=True, frozen=True)
class ExecutedToolCall:
    call: ToolCall
    result: ToolResult


@dataclass(slots=True, frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    provider_payload: Any
    reason_hint: str = ""
    continue_required: bool = False
    outcome: TurnOutcome = "completed"


@dataclass(slots=True, frozen=True)
class LoopPolicy:
    max_tool_rounds: int = 8
    max_tool_calls: int = 16
    max_repeated_call: int = 2
    max_tool_result_chars: int = 20_000
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> LoopPolicy:
        return cls(
            max_tool_rounds=_env_int("PUPU_TOOL_MAX_ROUNDS", 8, minimum=1, maximum=64),
            max_tool_calls=_env_int("PUPU_TOOL_MAX_CALLS", 16, minimum=1, maximum=256),
            max_repeated_call=_env_int(
                "PUPU_TOOL_MAX_REPEATED_CALL", 2, minimum=1, maximum=16
            ),
            max_tool_result_chars=_env_int(
                "PUPU_TOOL_MAX_RESULT_CHARS",
                20_000,
                minimum=1000,
                maximum=1_000_000,
            ),
            timeout_seconds=_env_float(
                "PUPU_TOOL_LOOP_TIMEOUT",
                120.0,
                minimum=1.0,
                maximum=3600.0,
            ),
        )


class ModelAdapter(Protocol):
    capabilities: ProviderCapabilities

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        image_urls: list[str],
        session_id: str,
    ) -> list[dict[str, Any]]: ...

    def complete_turn(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]],
        request_overrides: dict[str, Any] | None,
        task_name: str,
        audit_role: str,
        session_id: str,
    ) -> ModelTurn: ...

    def append_tool_results(
        self,
        messages: list[dict[str, Any]],
        turn: ModelTurn,
        results: list[ExecutedToolCall],
    ) -> list[dict[str, Any]]: ...

    def append_continuation(
        self,
        messages: list[dict[str, Any]],
        turn: ModelTurn,
    ) -> list[dict[str, Any]]: ...


class AgentRunError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        tool_execution_started: bool,
        model_rounds: int,
        tool_calls_executed: int,
        fallback_allowed: bool = True,
    ) -> None:
        super().__init__(message)
        self.tool_execution_started = tool_execution_started
        self.model_rounds = model_rounds
        self.tool_calls_executed = tool_calls_executed
        self.fallback_allowed = fallback_allowed


class AgentLoopLimitError(AgentRunError):
    """Raised when a configured loop safety budget is exhausted."""


class AgentRunner:
    def __init__(self, policy: LoopPolicy | None = None) -> None:
        self.policy = policy or LoopPolicy.from_env()

    def run(
        self,
        adapter: ModelAdapter,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_handler: ToolHandler | None = None,
        request_overrides: dict[str, Any] | None = None,
        task_name: str = "chat",
        audit_role: str = "chat",
        session_id: str = "default",
        image_urls: list[str] | None = None,
    ) -> str:
        deadline = time.monotonic() + self.policy.timeout_seconds
        deadline_token = _CURRENT_AGENT_DEADLINE.set(deadline)
        try:
            return self._run_loop(
                adapter,
                model=model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                tool_handler=tool_handler,
                request_overrides=request_overrides,
                task_name=task_name,
                audit_role=audit_role,
                session_id=session_id,
                image_urls=image_urls,
                deadline=deadline,
            )
        finally:
            _CURRENT_AGENT_DEADLINE.reset(deadline_token)

    def _run_loop(
        self,
        adapter: ModelAdapter,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]] | None,
        tool_handler: ToolHandler | None,
        request_overrides: dict[str, Any] | None,
        task_name: str,
        audit_role: str,
        session_id: str,
        image_urls: list[str] | None,
        deadline: float,
    ) -> str:
        tool_definitions = list(tools or [])
        if tool_definitions and not adapter.capabilities.tools:
            raise AgentRunError(
                "selected model provider does not support tool calls",
                tool_execution_started=False,
                model_rounds=0,
                tool_calls_executed=0,
            )
        if image_urls and not adapter.capabilities.vision:
            raise AgentRunError(
                "selected model provider does not support image input",
                tool_execution_started=False,
                model_rounds=0,
                tool_calls_executed=0,
            )

        transcript = adapter.prepare_messages(
            messages,
            image_urls=list(image_urls or []),
            session_id=session_id,
        )
        model_rounds = 0
        tool_rounds = 0
        tool_calls_executed = 0
        tool_execution_started = False
        repeated_calls: Counter[str] = Counter()

        while True:
            self._check_time_budget(
                deadline,
                tool_execution_started=tool_execution_started,
                model_rounds=model_rounds,
                tool_calls_executed=tool_calls_executed,
            )
            model_rounds += 1
            try:
                turn = adapter.complete_turn(
                    model=model,
                    system=system,
                    messages=transcript,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tool_definitions,
                    request_overrides=request_overrides,
                    task_name=task_name,
                    audit_role=audit_role,
                    session_id=session_id,
                )
            except AgentRunError:
                raise
            except Exception as exc:
                raise AgentRunError(
                    str(exc),
                    tool_execution_started=tool_execution_started,
                    model_rounds=model_rounds,
                    tool_calls_executed=tool_calls_executed,
                ) from exc
            self._check_time_budget(
                deadline,
                tool_execution_started=tool_execution_started,
                model_rounds=model_rounds,
                tool_calls_executed=tool_calls_executed,
            )

            if turn.tool_calls:
                if tool_handler is None:
                    raise AgentRunError(
                        "model requested a tool but no tool handler was supplied",
                        tool_execution_started=tool_execution_started,
                        model_rounds=model_rounds,
                        tool_calls_executed=tool_calls_executed,
                    )
                tool_rounds += 1
                if tool_rounds > self.policy.max_tool_rounds:
                    raise self._limit_error(
                        f"tool loop exceeded {self.policy.max_tool_rounds} rounds",
                        tool_execution_started,
                        model_rounds,
                        tool_calls_executed,
                    )

                if (
                    tool_calls_executed + len(turn.tool_calls)
                    > self.policy.max_tool_calls
                ):
                    raise self._limit_error(
                        f"tool loop exceeded {self.policy.max_tool_calls} calls",
                        tool_execution_started,
                        model_rounds,
                        tool_calls_executed,
                    )
                next_repeated_calls = repeated_calls.copy()
                for call in turn.tool_calls:
                    signature = _tool_call_signature(call)
                    next_repeated_calls[signature] += 1
                    if next_repeated_calls[signature] > self.policy.max_repeated_call:
                        raise self._limit_error(
                            f"tool loop repeated the same call more than "
                            f"{self.policy.max_repeated_call} times: {call.name}",
                            tool_execution_started,
                            model_rounds,
                            tool_calls_executed,
                        )
                repeated_calls = next_repeated_calls

                results: list[ExecutedToolCall] = []
                for call in turn.tool_calls:
                    if call.parse_error:
                        result = ToolResult(
                            content=f"工具参数无效：{call.parse_error}",
                            is_error=True,
                        )
                    else:
                        tool_execution_started = True
                        try:
                            raw_result = tool_handler(
                                call.name,
                                call.arguments,
                                turn.reason_hint or None,
                            )
                            result = _coerce_tool_result(raw_result)
                        except Exception as exc:
                            result = ToolResult(
                                content=f"工具执行失败：{type(exc).__name__}: {exc}",
                                is_error=True,
                            )
                    tool_calls_executed += 1
                    self._check_time_budget(
                        deadline,
                        tool_execution_started=tool_execution_started,
                        model_rounds=model_rounds,
                        tool_calls_executed=tool_calls_executed,
                    )
                    result = _limit_tool_result(
                        result,
                        self.policy.max_tool_result_chars,
                    )
                    results.append(ExecutedToolCall(call=call, result=result))

                try:
                    transcript = adapter.append_tool_results(transcript, turn, results)
                except AgentRunError:
                    raise
                except Exception as exc:
                    raise AgentRunError(
                        str(exc),
                        tool_execution_started=tool_execution_started,
                        model_rounds=model_rounds,
                        tool_calls_executed=tool_calls_executed,
                    ) from exc
                continue

            if turn.continue_required:
                tool_rounds += 1
                if tool_rounds > self.policy.max_tool_rounds:
                    raise self._limit_error(
                        f"model continuation exceeded {self.policy.max_tool_rounds} rounds",
                        tool_execution_started,
                        model_rounds,
                        tool_calls_executed,
                    )
                try:
                    transcript = adapter.append_continuation(transcript, turn)
                except AgentRunError:
                    raise
                except Exception as exc:
                    raise AgentRunError(
                        str(exc),
                        tool_execution_started=tool_execution_started,
                        model_rounds=model_rounds,
                        tool_calls_executed=tool_calls_executed,
                    ) from exc
                continue

            if turn.outcome != "completed":
                raise AgentRunError(
                    f"model turn ended with {turn.outcome}: {turn.stop_reason or 'unknown'}",
                    tool_execution_started=tool_execution_started,
                    model_rounds=model_rounds,
                    tool_calls_executed=tool_calls_executed,
                    fallback_allowed=False,
                )
            self._check_time_budget(
                deadline,
                tool_execution_started=tool_execution_started,
                model_rounds=model_rounds,
                tool_calls_executed=tool_calls_executed,
            )
            return turn.text.strip()

    def _check_time_budget(
        self,
        deadline: float,
        *,
        tool_execution_started: bool,
        model_rounds: int,
        tool_calls_executed: int,
    ) -> None:
        if time.monotonic() <= deadline:
            return
        raise self._limit_error(
            f"tool loop exceeded {self.policy.timeout_seconds:g}s",
            tool_execution_started,
            model_rounds,
            tool_calls_executed,
        )

    @staticmethod
    def _limit_error(
        message: str,
        tool_execution_started: bool,
        model_rounds: int,
        tool_calls_executed: int,
    ) -> AgentLoopLimitError:
        return AgentLoopLimitError(
            message,
            tool_execution_started=tool_execution_started,
            model_rounds=model_rounds,
            tool_calls_executed=tool_calls_executed,
        )


def remaining_agent_timeout(maximum: float | None = None) -> float | None:
    """Return the current run's remaining wall-clock budget."""

    deadline = _CURRENT_AGENT_DEADLINE.get()
    if deadline is None:
        return maximum
    remaining = max(0.001, deadline - time.monotonic())
    if maximum is None:
        return remaining
    return min(max(0.001, float(maximum)), remaining)


def _coerce_tool_result(value: ToolContent | ToolResult) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, list):
        return ToolResult(content=value)
    return ToolResult(content=str(value))


def _tool_call_signature(call: ToolCall) -> str:
    try:
        arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
    except Exception:
        arguments = repr(call.arguments)
    return f"{call.name}\0{arguments}"


def _limit_tool_result(result: ToolResult, max_chars: int) -> ToolResult:
    if isinstance(result.content, str):
        if len(result.content) <= max_chars:
            return result
        return ToolResult(
            content=result.content[:max_chars] + "\n...(工具结果过长，已截断)",
            is_error=result.is_error,
            structured_content=None,
        )
    try:
        encoded = json.dumps(result.content, ensure_ascii=False)
    except Exception:
        encoded = repr(result.content)
    if len(encoded) <= max_chars:
        return result
    return ToolResult(
        content=f"工具结果超过 {max_chars} 字符，已省略。",
        is_error=True,
        structured_content=None,
    )


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))
