"""Shared config loading helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .instance_context import get_current_instance_context
from .app_config import (
    default_owner_ids,
    default_private_allowed_ids,
    default_private_reply_mode,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "instances" / "_no_instance" / "instance.json"

# Used when config omits ``owner_ids`` and for new console instances.
DEFAULT_OWNER_IDS: list[str] = []
DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS = 60.0


def _safe_default_owner_ids() -> list[str]:
    try:
        return default_owner_ids()
    except Exception:
        return []


def _safe_default_private_allowed_ids() -> list[str]:
    try:
        return default_private_allowed_ids()
    except Exception:
        return []


def _safe_default_private_reply_mode() -> str:
    try:
        return default_private_reply_mode()
    except Exception:
        return "owner_only"


def get_config_path() -> Path:
    ctx = get_current_instance_context()
    if ctx is not None:
        return ctx.config_path
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
        return _safe_default_owner_ids() or list(DEFAULT_OWNER_IDS)
    if "owner_ids" in config:
        raw = config.get("owner_ids")
    else:
        raw = _safe_default_owner_ids() or DEFAULT_OWNER_IDS
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw]


def load_owner_id_set() -> set[str]:
    return set(load_owner_ids())


def load_private_allowed_ids() -> list[str]:
    try:
        config = load_config()
    except Exception:
        return _safe_default_private_allowed_ids()
    raw = (
        config.get("private_allowed_ids")
        if "private_allowed_ids" in config
        else _safe_default_private_allowed_ids()
    )
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw if str(value).strip()]


def load_private_reply_mode() -> str:
    try:
        config = load_config()
    except Exception:
        return _safe_default_private_reply_mode()
    raw = (
        config.get("private_reply_mode")
        if "private_reply_mode" in config
        else _safe_default_private_reply_mode()
    )
    mode = str(raw or "").strip().lower()
    return mode if mode in {"owner_only", "allowlist", "all"} else "owner_only"


def is_private_reply_allowed(user_id) -> bool:
    uid = str(user_id).strip()
    if not uid:
        return False
    mode = load_private_reply_mode()
    if mode == "all":
        return True
    if uid in load_owner_id_set():
        return True
    if mode == "allowlist":
        return uid in set(load_private_allowed_ids())
    return False


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
