"""Configuration helpers for PuPu's built-in semantic index."""

from __future__ import annotations

import os

DEFAULT_TOP_K = 5
DEFAULT_SOURCE_SUMMARY_LIMIT = 0
DEFAULT_EMBED_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBED_MODEL = "text-embedding-v4"
DEFAULT_TIMEOUT_SECONDS = 45.0


def _env_bool_auto(name: str, default: str = "auto") -> str:
    return os.environ.get(name, default).strip().lower() or default


def _truthy(value: str) -> bool:
    return value in {"1", "true", "yes", "on", "enabled"}


def _falsey(value: str) -> bool:
    return value in {"0", "false", "no", "off", "disabled"}


def semantic_enabled_env() -> str:
    return _env_bool_auto("PUPU_SEMANTIC_INDEX_ENABLED", "auto")


def semantic_api_key() -> str:
    return os.environ.get("PUPU_SEMANTIC_INDEX_EMBED_API_KEY", "").strip()


def semantic_base_url() -> str:
    return (
        os.environ.get("PUPU_SEMANTIC_INDEX_EMBED_BASE_URL", "").strip()
        or DEFAULT_EMBED_BASE_URL
    ).rstrip("/")


def semantic_model() -> str:
    return os.environ.get("PUPU_SEMANTIC_INDEX_EMBED_MODEL", "").strip() or DEFAULT_EMBED_MODEL


def semantic_timeout() -> float:
    raw = os.environ.get("PUPU_SEMANTIC_INDEX_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(3.0, min(180.0, float(raw)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def semantic_top_k() -> int:
    try:
        return max(1, int(os.environ.get("PUPU_SEMANTIC_INDEX_RETRIEVE_TOP_K", DEFAULT_TOP_K)))
    except Exception:
        return DEFAULT_TOP_K


def semantic_source_summary_limit() -> int:
    try:
        value = int(
            os.environ.get(
                "PUPU_SEMANTIC_INDEX_SOURCE_SUMMARY_LIMIT",
                str(DEFAULT_SOURCE_SUMMARY_LIMIT),
            )
        )
    except Exception:
        return DEFAULT_SOURCE_SUMMARY_LIMIT
    return max(0, value)


def is_semantic_index_enabled() -> bool:
    raw = semantic_enabled_env()
    if _falsey(raw):
        return False
    if not semantic_api_key():
        return False
    return True


def semantic_config_signature() -> tuple[str, str, str]:
    return (semantic_base_url(), semantic_model(), "key" if semantic_api_key() else "")


__all__ = [
    "DEFAULT_EMBED_BASE_URL",
    "DEFAULT_EMBED_MODEL",
    "DEFAULT_TOP_K",
    "is_semantic_index_enabled",
    "semantic_api_key",
    "semantic_base_url",
    "semantic_config_signature",
    "semantic_enabled_env",
    "semantic_model",
    "semantic_source_summary_limit",
    "semantic_timeout",
    "semantic_top_k",
]
