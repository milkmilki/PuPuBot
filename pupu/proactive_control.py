"""Runtime/config switch for proactive messaging."""

from __future__ import annotations

import json
import os

from .instance_context import get_current_instance_context


_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _parse_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def is_proactive_enabled(default: bool = True) -> bool:
    ctx = get_current_instance_context()
    if ctx is not None and ctx.config_path.is_file():
        try:
            cfg = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and "proactive_enabled" in cfg:
                return _parse_bool(cfg.get("proactive_enabled"), default)
        except Exception:
            pass
    return _parse_bool(os.environ.get("PUPU_PROACTIVE_ENABLED", ""), default)


def set_proactive_enabled(enabled: bool, *, persist: bool = True) -> None:
    value = "true" if enabled else "false"
    os.environ["PUPU_PROACTIVE_ENABLED"] = value
    ctx = get_current_instance_context()
    if not persist or ctx is None:
        return
    try:
        cfg = {}
        if ctx.config_path.is_file():
            loaded = json.loads(ctx.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        cfg["proactive_enabled"] = bool(enabled)
        ctx.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[pupu] proactive setting persist failed: {exc}")


__all__ = ["is_proactive_enabled", "set_proactive_enabled"]
