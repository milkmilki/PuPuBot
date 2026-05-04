"""In-memory wait-followup: should_wait=true schedules a timer and delivers a nudge via chat()."""

from __future__ import annotations

import threading
from typing import Callable

from .followup import WAIT_DELAY_SECONDS
from .followup_manager import cancel_timer, create_timer, has_timer

FOLLOWUP_SOURCE = "wait_followup"

_senders: dict[str, Callable[[str], None]] = {}
_senders_lock = threading.Lock()


def is_followup_eligible(session_id: str) -> bool:
    sid = str(session_id or "").strip()
    if sid == "owner":
        return True
    if sid.startswith("private_"):
        tail = sid[8:]
        return tail.isdigit()
    return False


def register_sender(session_id: str, sender: Callable[[str], None]) -> None:
    with _senders_lock:
        _senders[str(session_id)] = sender


def unregister_sender(session_id: str) -> None:
    with _senders_lock:
        _senders.pop(str(session_id), None)


def schedule_wait_timer(session_id: str) -> None:
    if not is_followup_eligible(session_id):
        return
    create_timer(session_id, float(WAIT_DELAY_SECONDS), _on_timer_fire, session_id)


def cancel_wait_timer(session_id: str) -> bool:
    return cancel_timer(session_id)


def has_wait_timer(session_id: str) -> bool:
    return has_timer(session_id)


def _on_timer_fire(session_id: str) -> None:
    from .agent import chat

    synthetic = (
        "[系统提醒] 你刚才那句还没收到对方回复，自然地追问/补一句即可，不要黏人。"
    )
    hint = "这是 wait_followup 触发的追问，不要重复你刚才说过的话。"
    text = chat(
        synthetic,
        session_id,
        is_admin=(session_id == "owner"),
        image_urls=None,
        reply_speed_hint=hint,
        message_source=FOLLOWUP_SOURCE,
    )
    sender = None
    with _senders_lock:
        sender = _senders.get(str(session_id))
    if sender and text and str(text).strip():
        try:
            sender(str(text).strip())
        except Exception as e:
            try:
                print(f"[pupu.dialogue_loop] sender failed: session={session_id} err={e}")
            except Exception:
                pass
