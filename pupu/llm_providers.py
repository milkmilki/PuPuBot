"""Provider adapters for Anthropic and OpenAI-compatible model calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

import httpx


ToolHandler = Callable[[str, dict, str | None], str | list[dict]]


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


@dataclass(slots=True)
class AnthropicProvider:
    client: object

    name: str = "anthropic"

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
        **_: object,
    ) -> str:
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
        response = self.client.messages.create(**create_kwargs)

        while response.stop_reason == "tool_use":
            if tool_handler is None:
                raise ProviderError("tool_use requested but no tool handler was supplied")

            tool_results = []
            reason_hint = collect_reason_hint(response.content)
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = tool_handler(block.name, block.input, reason_hint or None)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result if not isinstance(result, list) else result,
                    }
                )

            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
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
            response = self.client.messages.create(**create_kwargs)

        text = join_text_blocks(response.content).strip()
        if text:
            return text
        return _extract_anthropic_fallback_text(response)

    def json_task(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        max_tokens: int,
        temperature: float | None = None,
        request_overrides: dict | None = None,
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
                *_openai_messages(messages),
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
        **_: object,
    ) -> str:
        self._validate_config()
        payload = self._build_payload(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._post_chat_completion(payload, task_name="chat")

    def json_task(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        max_tokens: int,
        temperature: float | None = None,
        task_name: str = "json_task",
        **_: object,
    ) -> str:
        self._validate_config()
        payload = self._build_payload(
            system=system,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._post_chat_completion(payload, task_name=task_name)

    def _validate_config(self) -> None:
        if not self.endpoint:
            raise ProviderError(f"{self.name} endpoint is not configured")
        if not self.api_key:
            raise ProviderError(f"{self.name} API key is not configured")

    def _post_chat_completion(self, payload: dict, *, task_name: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        print(
            f"[pupu][llm] provider={self.name} task={task_name} "
            f"model={payload.get('model')} timeout={self.timeout_seconds}s"
        )
        try:
            response = httpx.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
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
                return text.strip()
            preview = (response.text or "").replace("\n", " ")[:500]
            raise ProviderError(
                f"{self.name} returned non-JSON response: {preview}"
            ) from exc

        text = _extract_openai_chat_text(data)
        if not text:
            preview = json.dumps(data, ensure_ascii=False)[:500]
            raise ProviderError(f"{self.name} returned empty completion: {preview}")
        return text.strip()


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


def _openai_messages(messages: list[dict]) -> list[dict[str, str]]:
    converted = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role not in {"system", "user", "assistant"}:
            role = "user"
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        converted.append({"role": role, "content": content})
    return converted


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
