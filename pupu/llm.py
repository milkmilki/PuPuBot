"""Unified model-provider facade for PuPu."""

from __future__ import annotations

import os

import anthropic

from .app_config import apply_app_config_env
from .llm_providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderError,
    collect_reason_hint,
    join_text_blocks,
)

apply_app_config_env()

MODEL = os.environ.get("PUPU_MODEL", "claude-opus-4-6")
JUDGE_MODEL = os.environ.get("PUPU_JUDGE_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_JUDGE_TEMPERATURE = 0.1

_client = None
_providers: dict[str, object] = {}
_last_provider_used: dict[str, str] = {}
_preflight_done = False

_ROLE_ENV = {
    "chat": "PUPU_CHAT_PROVIDER",
    "judge": "PUPU_JUDGE_PROVIDER",
    "maintenance": "PUPU_MAINTENANCE_PROVIDER",
    "proactive": "PUPU_PROACTIVE_PROVIDER",
}
SUPPORTED_PROVIDERS = ("anthropic", "xiaoshuoai", "deepseek")
_RESERVED_PROVIDERS = ("gemini",)
XIAOSHUOAI_ENDPOINT = (
    "https://www.gpt4novel.com/api/xiaoshuoai/ext/v1/chat/completions"
)
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/anthropic"


class ProviderConfigError(ProviderError):
    """Raised when the configured model provider is missing local setup."""


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


def role_temperature(role: str) -> float | None:
    env_name = f"PUPU_{role.upper()}_TEMPERATURE"
    raw = os.environ.get(env_name, "").strip()
    if raw:
        try:
            return max(0.0, min(1.0, float(raw)))
        except Exception:
            print(f"[pupu][llm] ignore invalid {env_name}={raw!r}; expected 0..1")
    if role == "judge":
        return DEFAULT_JUDGE_TEMPERATURE
    return None


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
    if configured_provider not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            f"unknown provider {configured_provider!r}; supported providers: "
            + ", ".join(SUPPORTED_PROVIDERS)
        )
    temperature = role_temperature(role)
    try:
        provider = get_provider(role)
        text = provider.chat_complete(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
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
            temperature=temperature,
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
    if configured_provider not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            f"unknown provider {configured_provider!r}; supported providers: "
            + ", ".join(SUPPORTED_PROVIDERS)
        )
    temperature = role_temperature(role)
    try:
        provider = get_provider(role)
        text = provider.json_task(
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=max_tokens,
            temperature=temperature,
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
            temperature=temperature,
            task_name=task_name,
        )
        _last_provider_used[role] = f"anthropic:{model} fallback_from={configured_provider}"
        return text


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
    return _DeepSeekAnthropicProvider(
        _deepseek_client(),
        default_model,
        _deepseek_request_overrides(),
    )


def _deepseek_request_overrides(effort_override: str = "") -> dict:
    effort = (effort_override or os.environ.get("PUPU_DEEPSEEK_EFFORT", "")).strip().lower()
    if not effort:
        return {}

    effort_aliases = {
        "low": "high",
        "medium": "high",
        "high": "high",
        "xhigh": "max",
        "max": "max",
    }
    normalized = effort_aliases.get(effort, "")
    if not normalized:
        print(f"[pupu][llm] ignore invalid PUPU_DEEPSEEK_EFFORT={effort!r}; expected high/max")
        return {}

    return {"output_config": {"effort": normalized}}


class _DeepSeekAnthropicProvider:
    def __init__(self, client: object, default_model: str, request_overrides: dict | None = None):
        self._provider = AnthropicProvider(client)
        self._default_model = default_model
        self._request_overrides = request_overrides or {}

    def chat_complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float | None = None,
        tools: list[dict] | None = None,
        tool_handler=None,
        session_id: str = "default",
        image_urls: list[str] | None = None,
        is_admin: bool = False,
        tool_exposure: str = "chat",
    ) -> str:
        model = self._default_model
        return self._provider.chat_complete(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_handler=tool_handler,
            request_overrides=self._request_overrides,
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
        temperature: float | None = None,
        task_name: str = "json_task",
    ) -> str:
        model = self._default_model
        request_overrides = self._request_overrides
        if task_name == "event_graph_migration":
            request_overrides = _deepseek_request_overrides(
                os.environ.get("PUPU_DEEPSEEK_EVENT_GRAPH_MIGRATION_EFFORT", "high")
            )
        return self._provider.json_task(
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=max_tokens,
            temperature=temperature,
            request_overrides=request_overrides,
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


def _has_env_value(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _provider_config_issue(provider_name: str) -> str | None:
    if provider_name == "anthropic":
        if _has_env_value("ANTHROPIC_API_KEY"):
            return None
        return (
            "Anthropic API key is not configured.\n"
            "Fill llm.anthropic.api_key in pupu.yaml, or set ANTHROPIC_API_KEY."
        )
    if provider_name == "deepseek":
        if _has_env_value("PUPU_DEEPSEEK_API_KEY") or _has_env_value("ANTHROPIC_API_KEY"):
            return None
        return (
            "DeepSeek API key is not configured.\n"
            "Fill llm.deepseek.api_key in pupu.yaml, or set PUPU_DEEPSEEK_API_KEY."
        )
    if provider_name == "xiaoshuoai":
        if _has_env_value("PUPU_XIAOSHUOAI_API_KEY"):
            return None
        return (
            "XiaoshuoAI API key is not configured.\n"
            "Fill llm.xiaoshuoai.api_key in pupu.yaml, or set PUPU_XIAOSHUOAI_API_KEY."
        )
    if provider_name not in SUPPORTED_PROVIDERS:
        return (
            f"Unknown provider {provider_name!r}.\n"
            f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    return None


def validate_model_provider_config(*, roles: tuple[str, ...] = ("chat",)) -> None:
    """Fail early when a required model provider cannot make requests."""
    issues: list[str] = []
    seen: set[str] = set()
    for role in roles:
        name = get_provider_name(role)
        key = f"{role}:{name}"
        if key in seen:
            continue
        seen.add(key)
        issue = _provider_config_issue(name)
        if issue:
            issues.append(f"- {role}: {issue}")
    if not issues:
        return
    raise ProviderConfigError(
        "Model provider is not ready.\n"
        + "\n".join(issues)
        + "\n\nOpen pupu.yaml and fill the relevant key. If the file is missing, the launcher will create it from pupu.yaml.example."
    )


def preflight_model_providers(*, require_chat: bool = False) -> None:
    global _preflight_done
    if _preflight_done:
        if require_chat:
            validate_model_provider_config(roles=("chat",))
        return
    _preflight_done = True

    if require_chat:
        validate_model_provider_config(roles=("chat",))


__all__ = [
    "DEFAULT_JUDGE_TEMPERATURE",
    "JUDGE_MODEL",
    "MODEL",
    "ProviderError",
    "SUPPORTED_PROVIDERS",
    "chat_complete",
    "collect_reason_hint",
    "get_client",
    "get_provider",
    "get_provider_name",
    "join_text_blocks",
    "json_task",
    "last_provider_label",
    "preflight_model_providers",
    "provider_label",
    "role_temperature",
    "set_provider_name",
]
