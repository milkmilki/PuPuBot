"""In-process followup timer manager.

This module stays at top level so importing followup helpers does not trigger
the heavier `pupu.tooling` package initialization chain.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

_timers: dict[str, threading.Timer] = {}
_work_q: "queue.Queue[tuple[Callable, tuple, dict]]" = queue.Queue()
_fired_q: "queue.Queue[str]" = queue.Queue()


def _worker_loop() -> None:
    while True:
        func, args, kwargs = _work_q.get()
        try:
            func(*args, **kwargs)
        except Exception as e:
            try:
                print(f"[pupu.followup_manager] callback error: {e}")
            except Exception:
                pass
        finally:
            _work_q.task_done()


_worker_thread = threading.Thread(
    target=_worker_loop, daemon=True, name="pupu-followup-worker"
)
_worker_thread.start()


def create_timer(session_id: str, delay_seconds: float, callback: Callable, *args, **kwargs) -> None:
    cancel_timer(session_id)

    def _on_fire() -> None:
        try:
            try:
                _fired_q.put(session_id)
            except Exception:
                pass
            try:
                _work_q.put((callback, args, kwargs))
            except Exception:
                pass
        except Exception as e:
            print(f"[pupu.followup_manager] enqueue failed: {e}")

    t = threading.Timer(delay_seconds, _on_fire)
    t.daemon = True
    _timers[session_id] = t
    t.start()


def cancel_timer(session_id: str) -> bool:
    t = _timers.pop(session_id, None)
    if not t:
        return False
    try:
        t.cancel()
    except Exception:
        pass
    return True


def has_timer(session_id: str) -> bool:
    return session_id in _timers


def drain_fired(max_items: int = 16) -> list[str]:
    items: list[str] = []
    for _ in range(max_items):
        try:
            sid = _fired_q.get_nowait()
        except Exception:
            break
        items.append(sid)
        _fired_q.task_done()
    return items
