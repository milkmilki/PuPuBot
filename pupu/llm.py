"""Unified model-provider facade for PuPu."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .llm_providers import (
    AnthropicProvider,
    CodexCliProvider,
    ProviderError,
    collect_reason_hint,
    join_text_blocks,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = os.environ.get("PUPU_MODEL", "claude-opus-4-6")
JUDGE_MODEL = os.environ.get("PUPU_JUDGE_MODEL", "claude-haiku-4-5-20251001")

_client = None
_providers: dict[str, object] = {}
_last_provider_used: dict[str, str] = {}
_root = Path(__file__).resolve().parent.parent
_preflight_done = False


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        )
    return _client


def get_provider_name(role: str) -> str:
    env_by_role = {
        "chat": "PUPU_CHAT_PROVIDER",
        "judge": "PUPU_JUDGE_PROVIDER",
        "maintenance": "PUPU_MAINTENANCE_PROVIDER",
        "proactive": "PUPU_PROACTIVE_PROVIDER",
    }
    env_name = env_by_role.get(role, "")
    if env_name:
        configured = os.environ.get(env_name, "").strip()
        if configured:
            return configured
    return "anthropic"


def get_provider(role: str):
    name = get_provider_name(role)
    if name == "gemini":
        raise ProviderError("gemini provider is reserved but not implemented yet")
    if name not in {"anthropic", "codex_cli"}:
        raise ProviderError(
            f"unknown provider {name!r}; supported providers: anthropic, codex_cli, gemini"
        )
    if name not in _providers:
        if name == "anthropic":
            _providers[name] = AnthropicProvider(get_client())
        else:
            _providers[name] = CodexCliProvider(workspace_root=_root)
    return _providers[name]


def provider_label(role: str, model: str | None = None) -> str:
    name = get_provider_name(role)
    if name == "anthropic" and model:
        return f"{name}:{model}"
    return name


def last_provider_label(role: str, model: str | None = None) -> str:
    return _last_provider_used.get(role) or provider_label(role, model)


def chat_complete(
    *,
    role: str,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    tool_handler=None,
    session_id: str = "default",
    image_urls: list[str] | None = None,
    is_admin: bool = False,
    tool_exposure: str = "chat",
) -> str:
    configured_provider = get_provider_name(role)
    try:
        provider = get_provider(role)
        text = provider.chat_complete(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
            tool_handler=tool_handler,
            session_id=session_id,
            image_urls=image_urls or [],
            is_admin=is_admin,
            tool_exposure=tool_exposure,
        )
        _last_provider_used[role] = provider_label(role, model)
        return text
    except Exception as exc:
        if configured_provider == "anthropic":
            raise
        print(
            f"[pupu][llm] provider={configured_provider} role={role} "
            f"failed={exc}; fallback=anthropic"
        )
        text = AnthropicProvider(get_client()).chat_complete(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
            tool_handler=tool_handler,
            session_id=session_id,
            image_urls=image_urls or [],
            is_admin=is_admin,
            tool_exposure=tool_exposure,
        )
        _last_provider_used[role] = f"anthropic:{model} fallback_from={configured_provider}"
        return text


def json_task(
    *,
    role: str,
    model: str,
    system: str,
    user_content: str,
    max_tokens: int,
    task_name: str,
) -> str:
    configured_provider = get_provider_name(role)
    try:
        provider = get_provider(role)
        text = provider.json_task(
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=max_tokens,
            task_name=task_name,
        )
        _last_provider_used[role] = provider_label(role, model)
        return text
    except Exception as exc:
        if configured_provider == "anthropic":
            raise
        print(
            f"[pupu][llm] provider={configured_provider} role={role} "
            f"task={task_name} failed={exc}; fallback=anthropic"
        )
        text = AnthropicProvider(get_client()).json_task(
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=max_tokens,
            task_name=task_name,
        )
        _last_provider_used[role] = f"anthropic:{model} fallback_from={configured_provider}"
        return text


def codex_cli_status() -> str:
    CodexCliProvider(workspace_root=_root).check_available()
    return "ok"


def preflight_model_providers() -> None:
    global _preflight_done
    if _preflight_done:
        return
    _preflight_done = True

    checked = set()
    for role in ("chat", "judge", "maintenance", "proactive"):
        name = get_provider_name(role)
        if name != "codex_cli" or name in checked:
            continue
        checked.add(name)
        try:
            codex_cli_status()
            print("[pupu][llm] codex_cli ready: logged in and executable")
        except Exception as exc:
            print(f"[pupu][llm] codex_cli unavailable: {exc}; fallback will use anthropic")


__all__ = [
    "JUDGE_MODEL",
    "MODEL",
    "ProviderError",
    "chat_complete",
    "codex_cli_status",
    "collect_reason_hint",
    "get_client",
    "get_provider",
    "get_provider_name",
    "join_text_blocks",
    "json_task",
    "last_provider_label",
    "preflight_model_providers",
    "provider_label",
]
