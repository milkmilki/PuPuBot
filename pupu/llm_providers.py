"""Provider adapters for Anthropic and OpenAI-compatible model calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from .image_context import get_image_data, remember_image_data
from .model_runtime import (
    AgentRunner,
    ExecutedToolCall,
    ModelTurn,
    ProviderCapabilities,
    ToolCall,
    ToolHandler,
    TurnOutcome,
    remaining_agent_timeout,
)
from .richmsg import download_image_as_base64

MAX_NATIVE_IMAGES = 4
MAX_NATIVE_IMAGE_BYTES = 5 * 1024 * 1024


class ProviderError(RuntimeError):
    """Raised when a provider cannot complete the requested model call."""


def join_text_blocks(content_blocks) -> str:
    return "".join(block.text for block in content_blocks if block.type == "text")


def collect_reason_hint(content_blocks) -> str:
    return " ".join(
        block.text.strip()
        for block in content_blocks
        if block.type == "text" and getattr(block, "text", "").strip()
    )


def _base64_source_size(value: str) -> int:
    return max(0, (len(str(value or "")) * 3) // 4)


def _messages_with_native_images(
    messages: list[dict],
    image_urls: list[str] | None,
    session_id: str,
) -> list[dict]:
    urls = [str(url or "").strip() for url in image_urls or [] if str(url or "").strip()]
    if not urls:
        return messages

    image_blocks: list[dict] = []
    for url in urls[:MAX_NATIVE_IMAGES]:
        image_data = get_image_data(session_id, url)
        if image_data is None:
            image_data = download_image_as_base64(url, max_bytes=MAX_NATIVE_IMAGE_BYTES)
        if image_data is None:
            print("[pupu][vision] native image download failed")
            continue
        b64, media_type = image_data
        if _base64_source_size(b64) > MAX_NATIVE_IMAGE_BYTES:
            print("[pupu][vision] native image skipped: source exceeds 5 MiB")
            continue
        remember_image_data(session_id, url, b64, media_type)
        image_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            }
        )

    if not image_blocks:
        return messages

    prepared = [dict(message) for message in messages]
    for index in range(len(prepared) - 1, -1, -1):
        if prepared[index].get("role") != "user":
            continue
        content = prepared[index].get("content", "")
        if isinstance(content, list):
            prepared[index]["content"] = [*image_blocks, *content]
        else:
            prepared[index]["content"] = [
                *image_blocks,
                {"type": "text", "text": str(content or "")},
            ]
        return prepared
    return messages


@dataclass(slots=True)
class AnthropicProvider:
    client: object

    name: str = "anthropic"
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        tools=True,
        vision=True,
        parallel_tool_calls=True,
        continuation=True,
    )

    def chat_complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        temperature: float | None = None,
        request_overrides: dict | None = None,
        audit_role: str = "chat",
        task_name: str = "chat",
        session_id: str = "default",
        image_urls: list[str] | None = None,
        **_: object,
    ) -> str:
        return AgentRunner().run(
            self,
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
        )

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        image_urls: list[str],
        session_id: str,
    ) -> list[dict[str, Any]]:
        return _messages_with_native_images(messages, image_urls, session_id)

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
    ) -> ModelTurn:
        create_kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools or None,
        }
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        if request_overrides:
            create_kwargs.update(request_overrides)
        request_timeout = remaining_agent_timeout()
        if request_timeout is not None:
            create_kwargs["timeout"] = request_timeout
        response = self.client.messages.create(**create_kwargs)
        text = join_text_blocks(response.content).strip()
        if not text:
            text = _extract_anthropic_fallback_text(response)
        tool_calls = tuple(
            ToolCall(
                id=str(block.id),
                name=str(block.name),
                arguments=dict(block.input or {}),
            )
            for block in response.content
            if block.type == "tool_use"
        )
        stop_reason = str(response.stop_reason or "")
        outcome = _anthropic_turn_outcome(stop_reason, has_tool_calls=bool(tool_calls))
        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            provider_payload=response.content,
            reason_hint=collect_reason_hint(response.content),
            continue_required=outcome == "continuation",
            outcome=outcome,
        )

    def append_tool_results(
        self,
        messages: list[dict[str, Any]],
        turn: ModelTurn,
        results: list[ExecutedToolCall],
    ) -> list[dict[str, Any]]:
        tool_results = []
        for executed in results:
            block = {
                "type": "tool_result",
                "tool_use_id": executed.call.id,
                "content": _anthropic_tool_result_content(executed.result.content),
            }
            if executed.result.is_error:
                block["is_error"] = True
            tool_results.append(block)
        return [
            *messages,
            {"role": "assistant", "content": turn.provider_payload},
            {"role": "user", "content": tool_results},
        ]

    def append_continuation(
        self,
        messages: list[dict[str, Any]],
        turn: ModelTurn,
    ) -> list[dict[str, Any]]:
        return [*messages, {"role": "assistant", "content": turn.provider_payload}]

    def json_task(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        max_tokens: int,
        temperature: float | None = None,
        request_overrides: dict | None = None,
        audit_role: str = "judge",
        session_id: str = "default",
        task_name: str = "json_task",
        **_: object,
    ) -> str:
        create_kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        if request_overrides:
            create_kwargs.update(request_overrides)
        response = self.client.messages.create(**create_kwargs)
        text = join_text_blocks(response.content).strip()
        if text:
            return text
        return _extract_anthropic_fallback_text(response)


@dataclass(slots=True)
class OpenAICompatibleProvider:
    """Call an OpenAI-compatible chat completions endpoint."""

    name: str
    endpoint: str
    api_key: str
    model: str
    timeout_seconds: float = 90.0
    temperature: float = 0.7
    reasoning_effort: str = ""
    thinking_enabled: bool = False
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        tools=True,
        vision=False,
        parallel_tool_calls=True,
    )

    def _build_payload(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float | None = None,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *[dict(message) for message in messages],
            ],
            "max_tokens": max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        if self.reasoning_effort:
            effort = "high" if self.reasoning_effort == "max" else self.reasoning_effort
            payload["reasoning_effort"] = effort
        if self.thinking_enabled:
            payload["extra_body"] = {"thinking": {"type": "enabled"}}
        return payload

    def chat_complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float | None = None,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        audit_role: str = "chat",
        session_id: str = "default",
        image_urls: list[str] | None = None,
        task_name: str = "chat",
        **_: object,
    ) -> str:
        return AgentRunner().run(
            self,
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_handler=tool_handler,
            task_name=task_name,
            audit_role=audit_role,
            session_id=session_id,
            image_urls=image_urls,
        )

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        image_urls: list[str],
        session_id: str,
    ) -> list[dict[str, Any]]:
        return _openai_messages(messages)

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
    ) -> ModelTurn:
        self._validate_config()
        payload = self._build_payload(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            payload["tools"] = [_openai_tool_definition(tool) for tool in tools]
        if request_overrides:
            payload.update(request_overrides)
        data = self._post_chat_completion_data(
            payload,
            task_name=task_name,
            audit_role=audit_role,
            session_id=session_id,
            timeout_seconds=remaining_agent_timeout(self.timeout_seconds),
        )
        message, stop_reason = _openai_response_message(data)
        text = _content_to_text(message.get("content")) or _content_to_text(
            message.get("refusal")
        )
        tool_calls = tuple(_openai_tool_calls(message))
        if not text and not tool_calls:
            preview = json.dumps(data, ensure_ascii=False)[:500]
            raise ProviderError(f"{self.name} returned empty completion: {preview}")
        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            provider_payload=message,
            reason_hint=text.strip(),
            outcome=_openai_turn_outcome(
                stop_reason,
                has_tool_calls=bool(tool_calls),
            ),
        )

    def append_tool_results(
        self,
        messages: list[dict[str, Any]],
        turn: ModelTurn,
        results: list[ExecutedToolCall],
    ) -> list[dict[str, Any]]:
        assistant_message = dict(turn.provider_payload or {})
        assistant_message["role"] = "assistant"
        tool_messages = []
        for executed in results:
            content = _openai_tool_result_content(executed.result.content)
            if executed.result.is_error:
                content = json.dumps(
                    {"error": True, "message": content},
                    ensure_ascii=False,
                )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": executed.call.id,
                    "content": content,
                }
            )
        return [*messages, assistant_message, *tool_messages]

    def append_continuation(
        self,
        messages: list[dict[str, Any]],
        turn: ModelTurn,
    ) -> list[dict[str, Any]]:
        return [*messages, {"role": "assistant", **dict(turn.provider_payload or {})}]

    def json_task(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        max_tokens: int,
        temperature: float | None = None,
        task_name: str = "json_task",
        audit_role: str = "judge",
        session_id: str = "default",
        **_: object,
    ) -> str:
        self._validate_config()
        payload = self._build_payload(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._post_chat_completion(
            payload,
            task_name=task_name,
            audit_role=audit_role,
            session_id=session_id,
        )

    def _validate_config(self) -> None:
        if not self.endpoint:
            raise ProviderError(f"{self.name} endpoint is not configured")
        if not self.api_key:
            raise ProviderError(f"{self.name} API key is not configured")

    def _post_chat_completion(
        self,
        payload: dict,
        *,
        task_name: str,
        audit_role: str,
        session_id: str,
    ) -> str:
        data = self._post_chat_completion_data(
            payload,
            task_name=task_name,
            audit_role=audit_role,
            session_id=session_id,
        )
        text = _extract_openai_chat_text(data)
        if not text:
            preview = json.dumps(data, ensure_ascii=False)[:500]
            raise ProviderError(f"{self.name} returned empty completion: {preview}")
        return text.strip()

    def _post_chat_completion_data(
        self,
        payload: dict,
        *,
        task_name: str,
        audit_role: str,
        session_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        print(
            f"[pupu][llm] provider={self.name} task={task_name} "
            f"model={payload.get('model')} timeout={self.timeout_seconds}s"
        )
        try:
            response = _post_and_raise(
                self.endpoint,
                headers=headers,
                payload=payload,
                timeout=timeout_seconds or self.timeout_seconds,
            )
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "").replace("\n", " ")[:500]
            raise ProviderError(
                f"{self.name} request failed with HTTP {exc.response.status_code}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            text = _extract_openai_sse_text(response.text)
            if text:
                return {"choices": [{"message": {"role": "assistant", "content": text.strip()}}]}
            preview = (response.text or "").replace("\n", " ")[:500]
            raise ProviderError(
                f"{self.name} returned non-JSON response: {preview}"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name} returned an invalid completion payload")
        return data


def _post_and_raise(
    endpoint: str,
    *,
    headers: dict,
    payload: dict,
    timeout: float,
) -> httpx.Response:
    response = httpx.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def _extract_anthropic_fallback_text(response: object) -> str:
    try:
        if hasattr(response, "to_dict"):
            raw = response.to_dict()
        elif hasattr(response, "raw"):
            raw = response.raw
        else:
            raw = getattr(response, "__dict__", None)
        if not isinstance(raw, dict):
            return ""
        choices = raw.get("choices") or raw.get("data") or []
        if not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        msg = first.get("message")
        if isinstance(msg, dict):
            reasoning = msg.get("reasoning_content") or msg.get("content")
            if isinstance(reasoning, str) and reasoning.strip():
                return reasoning.strip()
        text_candidate = first.get("text") or first.get("message", {}).get("content")
        if isinstance(text_candidate, str) and text_candidate.strip():
            return text_candidate.strip()
    except Exception:
        pass
    return ""


def _anthropic_turn_outcome(
    stop_reason: str,
    *,
    has_tool_calls: bool,
) -> TurnOutcome:
    if has_tool_calls or stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason == "pause_turn":
        return "continuation"
    if stop_reason in {"max_tokens", "model_context_window_exceeded"}:
        return "truncated"
    if stop_reason == "refusal":
        return "refused"
    if stop_reason in {"", "end_turn", "stop_sequence"}:
        return "completed"
    return "failed"


def _openai_turn_outcome(
    stop_reason: str,
    *,
    has_tool_calls: bool,
) -> TurnOutcome:
    if has_tool_calls or stop_reason in {"tool_calls", "function_call"}:
        return "tool_calls"
    if stop_reason == "length":
        return "truncated"
    if stop_reason in {"content_filter", "refusal"}:
        return "refused"
    if stop_reason in {"", "stop"}:
        return "completed"
    return "failed"


def _openai_messages(messages: list[dict]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        content = message.get("content", "")
        if not isinstance(content, (str, list)):
            content = json.dumps(content, ensure_ascii=False)
        converted_message: dict[str, Any] = {"role": role, "content": content}
        for key in ("tool_calls", "tool_call_id", "name"):
            if key in message:
                converted_message[key] = message[key]
        converted.append(converted_message)
    return converted


def _openai_tool_definition(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        return dict(tool)
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("input_schema") or {"type": "object"},
        },
    }


def _openai_response_message(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        preview = json.dumps(data, ensure_ascii=False)[:500]
        raise ProviderError(f"OpenAI-compatible provider returned no choices: {preview}")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        preview = json.dumps(data, ensure_ascii=False)[:500]
        raise ProviderError(f"OpenAI-compatible provider returned no message: {preview}")
    return message, str(choice.get("finish_reason") or "")


def _openai_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return calls
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict):
            continue
        call_id = str(raw_call.get("id") or f"tool-call-{index}")
        name = str(function.get("name") or "")
        raw_arguments = function.get("arguments")
        parse_error = ""
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                parsed = json.loads(str(raw_arguments or "{}"))
                if not isinstance(parsed, dict):
                    raise ValueError("tool arguments must decode to an object")
                arguments = parsed
            except (TypeError, ValueError) as exc:
                arguments = {}
                parse_error = str(exc)
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                parse_error=parse_error,
            )
        )
    return calls


def _openai_tool_result_content(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _anthropic_tool_result_content(content: object) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append({"type": "text", "text": str(block)})
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif block_type == "image" and block.get("data"):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(
                            block.get("mimeType")
                            or block.get("mime_type")
                            or "image/jpeg"
                        ),
                        "data": str(block.get("data") or ""),
                    },
                }
            )
        else:
            blocks.append(
                {
                    "type": "text",
                    "text": json.dumps(block, ensure_ascii=False),
                }
            )
    return blocks


def _extract_openai_chat_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message")
        if isinstance(message, dict):
            text = _content_to_text(message.get("content"))
            if text:
                return text
        text = _content_to_text(first.get("text"))
        if text:
            return text
        delta = first.get("delta")
        if isinstance(delta, dict):
            return _content_to_text(delta.get("content"))

    for key in ("content", "text", "message"):
        text = _content_to_text(data.get(key))
        if text:
            return text
    return ""


def _extract_openai_sse_text(raw_text: str) -> str:
    parts = []
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        text = _extract_openai_chat_text(data)
        if text:
            parts.append(text)
    return "".join(parts)


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""
