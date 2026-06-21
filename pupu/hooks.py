"""Small process-local hook layer for PuPu runtime events."""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .instance_context import InstanceContext, get_current_instance_context

HookCallback = Callable[["HookEvent"], Any]


@dataclass(frozen=True, slots=True)
class HookEvent:
    name: str
    payload: dict[str, Any]
    created_at: str
    context: InstanceContext | None = None

    @property
    def instance_id(self) -> str:
        return self.context.instance_id if self.context is not None else ""


_LOCK = threading.RLock()
_HOOKS: dict[str, list[HookCallback]] = {}


def register_hook(name: str, callback: HookCallback) -> Callable[[], None]:
    """Register ``callback`` for one hook name and return an unregister function."""

    event_name = str(name or "").strip()
    if not event_name:
        raise ValueError("hook name is required")
    if not callable(callback):
        raise TypeError("hook callback must be callable")
    with _LOCK:
        _HOOKS.setdefault(event_name, []).append(callback)

    def unregister() -> None:
        with _LOCK:
            callbacks = _HOOKS.get(event_name)
            if not callbacks:
                return
            try:
                callbacks.remove(callback)
            except ValueError:
                return
            if not callbacks:
                _HOOKS.pop(event_name, None)

    return unregister


def clear_hooks() -> None:
    """Remove all process-local hooks. Intended for tests and controlled reloads."""

    with _LOCK:
        _HOOKS.clear()


def list_hooks() -> dict[str, int]:
    with _LOCK:
        return {name: len(callbacks) for name, callbacks in _HOOKS.items()}


def _callbacks_for(name: str) -> list[HookCallback]:
    with _LOCK:
        return list(_HOOKS.get(name, ()))


def _log_hook_error(event: HookEvent, exc: BaseException) -> None:
    try:
        print(
            "[pupu][hook] "
            f"name={event.name} instance={event.instance_id or '<none>'} "
            f"error={type(exc).__name__}: {exc}"
        )
    except Exception:
        pass


async def emit_hook(name: str, payload: dict[str, Any] | None = None) -> HookEvent:
    """Emit one hook event.

    Hook failures are isolated: an observer can log or extend behavior, but it
    must never prevent the bot from starting, stopping, or replying.
    """

    event = HookEvent(
        name=str(name or "").strip(),
        payload=dict(payload or {}),
        created_at=datetime.now().isoformat(timespec="seconds"),
        context=get_current_instance_context(),
    )
    if not event.name:
        return event
    for callback in _callbacks_for(event.name):
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _log_hook_error(event, exc)
    return event


async def emit_instance_status(
    status: str,
    *,
    runtime: str = "actor",
    error: str = "",
    details: dict[str, Any] | None = None,
) -> HookEvent:
    ctx = get_current_instance_context()
    payload: dict[str, Any] = {
        "status": str(status or "").strip(),
        "runtime": str(runtime or "").strip(),
        "error": str(error or "").strip(),
    }
    if ctx is not None:
        payload.update(
            {
                "instance_id": ctx.instance_id,
                "display_name": ctx.display_name,
                "qq_mode": ctx.qq_mode,
                "instance_dir": str(ctx.instance_dir),
            }
        )
    if details:
        payload["details"] = dict(details)
    return await emit_hook("instance.status", payload)


__all__ = [
    "HookEvent",
    "clear_hooks",
    "emit_hook",
    "emit_instance_status",
    "list_hooks",
    "register_hook",
]
