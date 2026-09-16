"""Explicit per-instance runtime context."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True, slots=True)
class InstanceContext:
    instance_id: str
    instance_dir: Path
    display_name: str
    qq_mode: str
    config_path: Path
    persona_path: Path
    db_path: Path
    data_dir: Path
    logs_dir: Path

    @classmethod
    def from_instance_dir(cls, instance_dir: str | os.PathLike[str]) -> "InstanceContext":
        inst = Path(instance_dir).expanduser().resolve()
        config_path = inst / "instance.json"
        config: dict = {}
        if config_path.is_file():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            except (OSError, json.JSONDecodeError):
                config = {}
        data_dir = inst / "data"
        display_name = str(config.get("display_name") or inst.name).strip() or inst.name
        qq_mode = str(config.get("qq_mode") or "cli").strip().lower() or "cli"
        return cls(
            instance_id=inst.name,
            instance_dir=inst,
            display_name=display_name,
            qq_mode=qq_mode,
            config_path=config_path,
            persona_path=inst / "persona.json",
            db_path=data_dir / "pupu.db",
            data_dir=data_dir,
            logs_dir=data_dir / "logs",
        )

    @contextmanager
    def activate(self) -> Iterator["InstanceContext"]:
        with activate_instance_context(self):
            yield self


_CURRENT_INSTANCE_CONTEXT: ContextVar[InstanceContext | None] = ContextVar(
    "pupu_current_instance_context",
    default=None,
)


def get_current_instance_context() -> InstanceContext | None:
    return _CURRENT_INSTANCE_CONTEXT.get()


def require_current_instance_context() -> InstanceContext:
    ctx = get_current_instance_context()
    if ctx is None:
        raise RuntimeError(
            "PuPu instance context is not active; choose an instance or start with --dir."
        )
    return ctx


@contextmanager
def activate_instance_context(context: InstanceContext) -> Iterator[InstanceContext]:
    token = _CURRENT_INSTANCE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_INSTANCE_CONTEXT.reset(token)


def activate_instance_context_global(context: InstanceContext) -> None:
    """Set the current context for long-lived entrypoints."""

    _CURRENT_INSTANCE_CONTEXT.set(context)


def clear_instance_context_global() -> None:
    """Clear the process-global context used by long-lived entrypoints/tests."""

    _CURRENT_INSTANCE_CONTEXT.set(None)


__all__ = [
    "InstanceContext",
    "activate_instance_context",
    "activate_instance_context_global",
    "clear_instance_context_global",
    "get_current_instance_context",
    "require_current_instance_context",
]
