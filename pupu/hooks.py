"""Small process-local hook layer for PuPu runtime events."""

from __future__ import annotations

import inspect
import asyncio
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


def _build_event(name: str, payload: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        name=str(name or "").strip(),
        payload=dict(payload or {}),
        created_at=datetime.now().isoformat(timespec="seconds"),
        context=get_current_instance_context(),
    )


def _log_hook_error(event: HookEvent, exc: BaseException) -> None:
    try:
        print(
            "[pupu][hook] "
            f"name={event.name} instance={event.instance_id or '<none>'} "
            f"error={type(exc).__name__}: {exc}"
        )
    except Exception:
        pass


async def _await_hook_result(event: HookEvent, result: Any) -> None:
    try:
        await result
    except Exception as exc:
        _log_hook_error(event, exc)


async def emit_hook(name: str, payload: dict[str, Any] | None = None) -> HookEvent:
    """Emit one hook event.

    Hook failures are isolated: an observer can log or extend behavior, but it
    must never prevent the bot from starting, stopping, or replying.
    """

    event = _build_event(name, payload)
    if not event.name:
        return event
    for callback in _callbacks_for(event.name):
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                await _await_hook_result(event, result)
        except Exception as exc:
            _log_hook_error(event, exc)
    return event


def emit_hook_sync(name: str, payload: dict[str, Any] | None = None) -> HookEvent:
    """Emit one hook event from synchronous code."""

    event = _build_event(name, payload)
    if not event.name:
        return event
    for callback in _callbacks_for(event.name):
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(_await_hook_result(event, result))
                else:
                    loop.create_task(_await_hook_result(event, result))
        except Exception as exc:
            _log_hook_error(event, exc)
    return event


def _base_session_payload(
    *,
    context_session: str,
    identity_session: str,
    source: str = "",
) -> dict[str, Any]:
    return {
        "context_session": str(context_session or ""),
        "identity_session": str(identity_session or ""),
        "source": str(source or ""),
    }


def _preview(value: object, limit: int = 120) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def emit_chat_started(
    *,
    context_session: str,
    identity_session: str,
    source: str,
    user_input: str,
    image_count: int = 0,
    persist_user: bool = True,
    speaker_key: str = "",
    speaker_name: str = "",
    speaker_qq: str = "",
) -> HookEvent:
    payload = _base_session_payload(
        context_session=context_session,
        identity_session=identity_session,
        source=source,
    )
    payload.update(
        {
            "input_preview": _preview(user_input),
            "input_chars": len(str(user_input or "")),
            "image_count": int(image_count or 0),
            "persist_user": bool(persist_user),
            "speaker_key": str(speaker_key or ""),
            "speaker_name": str(speaker_name or ""),
            "speaker_qq": str(speaker_qq or ""),
        }
    )
    return emit_hook_sync("chat.started", payload)


def emit_chat_reply_created(
    *,
    context_session: str,
    identity_session: str,
    source: str,
    reply_text: str,
    should_wait: bool,
) -> HookEvent:
    payload = _base_session_payload(
        context_session=context_session,
        identity_session=identity_session,
        source=source,
    )
    payload.update(
        {
            "reply_preview": _preview(reply_text),
            "reply_chars": len(str(reply_text or "")),
            "should_wait": bool(should_wait),
        }
    )
    return emit_hook_sync("chat.reply_created", payload)


def emit_chat_error(
    *,
    context_session: str,
    identity_session: str,
    source: str,
    error: BaseException,
) -> HookEvent:
    payload = _base_session_payload(
        context_session=context_session,
        identity_session=identity_session,
        source=source,
    )
    payload["error"] = f"{type(error).__name__}: {error}"
    return emit_hook_sync("chat.error", payload)


def emit_memory_review_started(
    *,
    context_session: str,
    identity_session: str,
    trigger: str,
    message_count: int,
    start_msg_id: int,
    end_msg_id: int,
) -> HookEvent:
    payload = _base_session_payload(
        context_session=context_session,
        identity_session=identity_session,
        source="chat",
    )
    payload.update(
        {
            "trigger": str(trigger or ""),
            "message_count": int(message_count or 0),
            "start_msg_id": int(start_msg_id or 0),
            "end_msg_id": int(end_msg_id or 0),
        }
    )
    return emit_hook_sync("memory.review_started", payload)


def emit_memory_review_finished(
    *,
    context_session: str,
    identity_session: str,
    status: str,
    trigger: str = "",
    message_count: int = 0,
    start_msg_id: int = 0,
    end_msg_id: int = 0,
    summary_chars: int = 0,
    fact_updates: int = 0,
    person_facts: int = 0,
    event_updates: int = 0,
    task_updates: int = 0,
    error: str = "",
) -> HookEvent:
    payload = _base_session_payload(
        context_session=context_session,
        identity_session=identity_session,
        source="chat",
    )
    payload.update(
        {
            "status": str(status or ""),
            "trigger": str(trigger or ""),
            "message_count": int(message_count or 0),
            "start_msg_id": int(start_msg_id or 0),
            "end_msg_id": int(end_msg_id or 0),
            "summary_chars": int(summary_chars or 0),
            "fact_updates": int(fact_updates or 0),
            "person_facts": int(person_facts or 0),
            "event_updates": int(event_updates or 0),
            "task_updates": int(task_updates or 0),
            "error": str(error or ""),
        }
    )
    return emit_hook_sync("memory.review_finished", payload)


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
    "emit_chat_error",
    "emit_chat_reply_created",
    "emit_chat_started",
    "emit_hook",
    "emit_hook_sync",
    "emit_instance_status",
    "emit_memory_review_finished",
    "emit_memory_review_started",
    "list_hooks",
    "register_hook",
]
