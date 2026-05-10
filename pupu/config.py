"""Shared config loading helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# Used when config omits ``owner_ids`` and for new console instances.
DEFAULT_OWNER_IDS: list[str] = ["424225912"]
DEFAULT_ARBITER_URL = "http://127.0.0.1:18079/api/group_arbitrate"
DEFAULT_ARBITER_BASE_URL = "http://127.0.0.1:18079"
DEFAULT_ARBITER_TIMEOUT_SECONDS = 300.0
DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS = 60.0
DEFAULT_ARBITER_SUBSCRIBE_TIMEOUT_SECONDS = 30.0


def get_config_path() -> Path:
    override = os.environ.get("PUPU_CONFIG_PATH")
    if override:
        return Path(override)
    return CONFIG_PATH


def load_config() -> dict:
    path = get_config_path()
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def save_config(config: dict) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def load_owner_ids() -> list[str]:
    try:
        config = load_config()
    except Exception:
        return list(DEFAULT_OWNER_IDS)
    return [str(value) for value in config.get("owner_ids", DEFAULT_OWNER_IDS)]


def load_owner_id_set() -> set[str]:
    return set(load_owner_ids())


def load_first_numeric_owner_id() -> int | None:
    for owner_id in load_owner_ids():
        if owner_id.isdigit():
            return int(owner_id)
    return None


def load_open_group_ids() -> set[str]:
    try:
        config = load_config()
    except Exception:
        return set()
    raw = config.get("open_groups", [])
    if not isinstance(raw, list):
        return set()
    return {str(value).strip() for value in raw if str(value).strip()}


def load_bot_id() -> str:
    try:
        config = load_config()
    except Exception:
        return ""
    return str(config.get("bot_id") or "").strip()


def load_arbiter_url() -> str:
    try:
        config = load_config()
    except Exception:
        return DEFAULT_ARBITER_URL
    return str(config.get("arbiter_url") or DEFAULT_ARBITER_URL).strip()


def load_arbiter_base_url() -> str:
    """Base URL for the centralized-debounce arbiter (``/api/observe`` etc.).

    Falls back to deriving the base from ``arbiter_url`` so single-config
    setups keep working without a new key.
    """
    try:
        config = load_config()
    except Exception:
        return DEFAULT_ARBITER_BASE_URL
    raw = str(config.get("arbiter_base_url") or "").strip()
    if raw:
        return raw.rstrip("/")
    legacy = str(config.get("arbiter_url") or DEFAULT_ARBITER_URL).strip()
    for suffix in ("/api/group_arbitrate", "/api/observe", "/api/await_decision"):
        if legacy.endswith(suffix):
            return legacy[: -len(suffix)].rstrip("/")
    return legacy.rstrip("/") or DEFAULT_ARBITER_BASE_URL


def load_arbiter_subscribe_timeout_seconds() -> float:
    """Long-poll timeout used by the arbiter decision subscriber."""
    raw_env = os.environ.get("PUPU_ARBITER_SUBSCRIBE_TIMEOUT_SEC", "").strip()
    if raw_env:
        try:
            return max(1.0, min(120.0, float(raw_env)))
        except ValueError:
            pass
    try:
        config = load_config()
        raw = config.get("arbiter_subscribe_timeout_seconds")
        if raw is not None:
            return max(1.0, min(120.0, float(raw)))
    except Exception:
        pass
    return DEFAULT_ARBITER_SUBSCRIBE_TIMEOUT_SECONDS


def load_arbiter_timeout_seconds() -> float:
    """HTTP client timeout for POST /api/group_arbitrate (arbiter may call LLM; keep generous)."""
    raw_env = os.environ.get("PUPU_ARBITER_TIMEOUT", "").strip()
    if raw_env:
        try:
            return max(5.0, min(600.0, float(raw_env)))
        except ValueError:
            pass
    try:
        config = load_config()
        raw = config.get("arbiter_timeout_seconds")
        if raw is not None:
            return max(5.0, min(600.0, float(raw)))
    except Exception:
        pass
    return DEFAULT_ARBITER_TIMEOUT_SECONDS


def load_peer_config() -> dict:
    try:
        config = load_config()
    except Exception:
        return {}
    peer = config.get("peer") or {}
    return peer if isinstance(peer, dict) else {}


def load_open_group_debounce_seconds() -> float:
    try:
        config = load_config()
        value = float(config.get("debounce_seconds_open_group", DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS))
    except Exception:
        value = DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS
    # Allow long open-group silences (e.g. 120–300s); cap avoids absurd values.
    return max(5.0, min(600.0, value))


def load_max_consecutive_bot_turns() -> int:
    """Same-named limit sent to group arbiter; ``0`` means unlimited (skip cap)."""
    try:
        config = load_config()
        raw = config.get("max_consecutive_bot_turns", 0)
        value = int(raw)
    except Exception:
        value = 0
    return max(0, min(99, value))
