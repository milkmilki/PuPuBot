"""Runtime/config switch for proactive messaging."""

from __future__ import annotations

import os
from pathlib import Path

from .instance_context import get_current_instance_context


_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_file_path() -> Path | None:
    ctx = get_current_instance_context()
    if ctx is not None:
        return ctx.instance_dir / ".env.qq"
    return None


def _parse_bool(value: object, default: bool = True) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def is_proactive_enabled(default: bool = True) -> bool:
    return _parse_bool(os.environ.get("PUPU_PROACTIVE_ENABLED", ""), default)


def set_proactive_enabled(enabled: bool, *, persist: bool = True) -> None:
    value = "true" if enabled else "false"
    os.environ["PUPU_PROACTIVE_ENABLED"] = value
    path = _env_file_path()
    if persist and path is not None:
        _set_env_file_value(path, "PUPU_PROACTIVE_ENABLED", value)


def _set_env_file_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    prefix = f"{key}="
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[index] = replacement
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = ["is_proactive_enabled", "set_proactive_enabled"]
