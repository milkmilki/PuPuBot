"""In-memory wait-followup timers with per-instance context capture."""

from __future__ import annotations

import threading
from typing import Callable

from .followup import WAIT_DELAY_SECONDS
from .followup_manager import cancel_timer, create_timer, has_timer
from .instance_context import (
    InstanceContext,
    activate_instance_context,
    get_current_instance_context,
)
from .message_sources import WAIT_FOLLOWUP
from .sessions import OWNER_SESSION

_senders: dict[str, Callable[[str], None]] = {}
_sender_contexts: dict[str, InstanceContext | None] = {}
_senders_lock = threading.Lock()


def is_followup_eligible(session_id: str) -> bool:
    sid = str(session_id or "").strip()
    if sid == OWNER_SESSION:
        return True
    if sid.startswith("private_"):
        tail = sid[8:]
        return tail.isdigit()
    return False


def register_sender(session_id: str, sender: Callable[[str], None]) -> None:
    key = str(session_id)
    with _senders_lock:
        _senders[key] = sender
        _sender_contexts[key] = get_current_instance_context()


def unregister_sender(session_id: str) -> None:
    key = str(session_id)
    with _senders_lock:
        _senders.pop(key, None)
        _sender_contexts.pop(key, None)


def schedule_wait_timer(session_id: str) -> None:
    if not is_followup_eligible(session_id):
        return
    create_timer(session_id, float(WAIT_DELAY_SECONDS), _on_timer_fire, session_id)


def cancel_wait_timer(session_id: str) -> bool:
    return cancel_timer(session_id)


def has_wait_timer(session_id: str) -> bool:
    return has_timer(session_id)


def _deliver_followup(session_id: str, sender: Callable[[str], None] | None) -> None:
    from .agent import chat

    synthetic = (
        "[????] ????????????????????????????????"
    )
    hint = "?? wait_followup ??????????????????"
    text = chat(
        synthetic,
        session_id,
        is_admin=(session_id == OWNER_SESSION),
        image_urls=None,
        reply_speed_hint=hint,
        message_source=WAIT_FOLLOWUP,
    )
    if sender and text and str(text).strip():
        try:
            sender(str(text).strip())
        except Exception as e:
            try:
                print(f"[pupu.dialogue_loop] sender failed: session={session_id} err={e}")
            except Exception:
                pass


def _on_timer_fire(session_id: str) -> None:
    key = str(session_id)
    with _senders_lock:
        sender = _senders.get(key)
        ctx = _sender_contexts.get(key)
    if ctx is None:
        _deliver_followup(session_id, sender)
        return
    with activate_instance_context(ctx):
        _deliver_followup(session_id, sender)
