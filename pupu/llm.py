"""Unified model-provider facade for PuPu."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .llm_providers import (
    AnthropicProvider,
    CodexCliProvider,
    OpenAICompatibleProvider,
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

_ROLE_ENV = {
    "chat": "PUPU_CHAT_PROVIDER",
    "judge": "PUPU_JUDGE_PROVIDER",
    "maintenance": "PUPU_MAINTENANCE_PROVIDER",
    "proactive": "PUPU_PROACTIVE_PROVIDER",
}
SUPPORTED_PROVIDERS = ("anthropic", "codex_cli", "xiaoshuoai", "deepseek")
_RESERVED_PROVIDERS = ("gemini",)
XIAOSHUOAI_ENDPOINT = (
    "https://www.gpt4novel.com/api/xiaoshuoai/ext/v1/chat/completions"
)
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/anthropic"


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        )
    return _client


def get_provider_name(role: str) -> str:
    env_name = _ROLE_ENV.get(role, "")
    if env_name:
        configured = os.environ.get(env_name, "").strip()
        if configured:
            return configured
    return "anthropic"


def set_provider_name(role: str, provider_name: str) -> None:
    if role not in _ROLE_ENV:
        raise ProviderError(
            f"unknown provider role {role!r}; supported roles: {', '.join(_ROLE_ENV)}"
        )
    provider_name = provider_name.strip()
    if provider_name in _RESERVED_PROVIDERS:
        raise ProviderError(f"{provider_name} provider is reserved but not implemented yet")
    if provider_name not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            f"unknown provider {provider_name!r}; supported providers: "
            + ", ".join(SUPPORTED_PROVIDERS)
        )
    os.environ[_ROLE_ENV[role]] = provider_name
    _last_provider_used.pop(role, None)


def get_provider(role: str):
    name = get_provider_name(role)
    if name == "gemini":
        raise ProviderError("gemini provider is reserved but not implemented yet")
    if name not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            f"unknown provider {name!r}; supported providers: "
            + ", ".join((*SUPPORTED_PROVIDERS, *_RESERVED_PROVIDERS))
        )
    if name not in _providers:
        if name == "anthropic":
            _providers[name] = AnthropicProvider(get_client())
        elif name == "codex_cli":
            _providers[name] = CodexCliProvider(workspace_root=_root)
        elif name == "xiaoshuoai":
            _providers[name] = _xiaoshuoai_provider()
        elif name == "deepseek":
            _providers[name] = _deepseek_provider()
    return _providers[name]


def provider_label(role: str, model: str | None = None) -> str:
    name = get_provider_name(role)
    if name == "anthropic" and model:
        return f"{name}:{model}"
    if name == "xiaoshuoai":
        provider_model = os.environ.get(_provider_model_env(name), "").strip()
        return f"{name}:{provider_model or 'default'}"
    if name == "deepseek":
        return f"{name}:{os.environ.get('PUPU_DEEPSEEK_MODEL', 'deepseek-v4-pro').strip()}"
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


def _xiaoshuoai_provider() -> OpenAICompatibleProvider:
    return _openai_compatible_provider(
        name="xiaoshuoai",
        env_prefix="PUPU_XIAOSHUOAI",
        default_base_url=XIAOSHUOAI_ENDPOINT,
        default_model="xiaoshuoai",
    )


def _deepseek_client():
    return anthropic.Anthropic(
        api_key=os.environ.get("PUPU_DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"),
        base_url=os.environ.get("PUPU_DEEPSEEK_BASE_URL", DEEPSEEK_ENDPOINT),
    )


def _deepseek_provider():
    default_model = os.environ.get("PUPU_DEEPSEEK_MODEL", "deepseek-v4-pro").strip() or "deepseek-v4-pro"
    return _DeepSeekAnthropicProvider(_deepseek_client(), default_model)


class _DeepSeekAnthropicProvider:
    def __init__(self, client: object, default_model: str):
        self._provider = AnthropicProvider(client)
        self._default_model = default_model

    def chat_complete(
        self,
        *,
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
        if not model:
            model = self._default_model
        return self._provider.chat_complete(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
            tool_handler=tool_handler,
            session_id=session_id,
            image_urls=image_urls,
            is_admin=is_admin,
            tool_exposure=tool_exposure,
        )

    def json_task(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        max_tokens: int,
        task_name: str = "json_task",
    ) -> str:
        if not model:
            model = self._default_model
        return self._provider.json_task(
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=max_tokens,
            task_name=task_name,
        )


def _openai_compatible_provider(
    *,
    name: str,
    env_prefix: str,
    default_base_url: str,
    default_model: str,
    default_reasoning_effort: str = "",
    default_thinking_enabled: bool = False,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name=name,
        endpoint=_resolve_openai_endpoint(
            os.environ.get(f"{env_prefix}_BASE_URL", default_base_url).strip(),
        ),
        api_key=os.environ.get(f"{env_prefix}_API_KEY", "").strip(),
        model=os.environ.get(f"{env_prefix}_MODEL", default_model).strip(),
        timeout_seconds=_env_float(f"{env_prefix}_TIMEOUT", 90.0),
        temperature=_env_float(f"{env_prefix}_TEMPERATURE", 0.7),
        reasoning_effort=os.environ.get(
            f"{env_prefix}_REASONING_EFFORT", default_reasoning_effort
        ).strip(),
        thinking_enabled=_env_bool(
            f"{env_prefix}_THINKING", default_thinking_enabled
        ),
    )


def _provider_model_env(name: str) -> str:
    if name == "deepseek":
        return "PUPU_DEEPSEEK_MODEL"
    return "PUPU_XIAOSHUOAI_MODEL"


def _resolve_openai_endpoint(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return base_url + "/chat/completions"
    return base_url + "/chat/completions"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


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
    "SUPPORTED_PROVIDERS",
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
    "set_provider_name",
]
