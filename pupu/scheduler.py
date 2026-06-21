"""Poll and run due scheduled tasks through transport-neutral senders."""

import asyncio
import os
import threading
from datetime import datetime

from .memory import finalize_scheduled_task, get_due_scheduled_tasks, get_recent_messages
from .message_sources import SCHEDULED, WAIT_FOLLOWUP
from .sessions import OWNER_SESSION

_scheduler_lock = threading.Lock()


def _sched_debug() -> bool:
    return str(os.environ.get("PUPU_DEBUG_SCHEDULED_TASKS", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _scheduled_user_message(task: dict) -> str:
    title = (task.get("title") or "提醒").strip()
    inst = (task.get("instruction") or "").strip()
    return f"[定时任务「{title}」]\n{inst}"


def _latest_message_is_user(session_id: str) -> bool:
    recent = get_recent_messages(1, session_id)
    if not recent:
        return False
    return str(recent[-1].get("role") or "") == "user"


def _scheduled_identity_session(context_session: str) -> str:
    """Scheduled tasks are delivered by context; unknown group owners use owner identity."""
    sid = str(context_session or "")
    if sid == OWNER_SESSION or sid.startswith("private_"):
        return sid
    return OWNER_SESSION


def _is_wait_followup_task(task: dict) -> bool:
    """Legacy DB tasks from an older design; in-memory timers replace these."""
    return str(task.get("title") or "").strip().lower().startswith(WAIT_FOLLOWUP)


def _finalize_due_task(task: dict) -> None:
    tid = task["id"]
    old_run = task["run_at"]
    rk = task.get("repeat_kind") or "once"
    print(
        "[pupu][scheduled-debug] scheduler_finalize_call "
        f"task_id={tid} session={task.get('session_id')} old_run_at={old_run} repeat={rk} interval={task.get('interval_seconds')}"
    )
    with _scheduler_lock:
        ok = finalize_scheduled_task(tid, old_run, rk, task.get("interval_seconds"))
    if not ok:
        print(f"[pupu] scheduled task #{tid}: finalize skipped (stale run_at?)")


async def _run_due_tasks_with_sender(send_func) -> None:
    """Run one scheduler tick using a transport-neutral async sender."""
    from .agent import chat

    now_iso = datetime.now().isoformat(timespec="seconds")
    with _scheduler_lock:
        tasks = [
            task
            for task in get_due_scheduled_tasks(now_iso, 10)
            if not _is_wait_followup_task(task)
        ]
    if tasks:
        brief = "; ".join(
            f"id={task['id']} session={task['session_id']} run_at={task['run_at']} repeat={task.get('repeat_kind')} title={str(task.get('title') or '')[:18]}"
            for task in tasks
        )
        print(
            "[pupu][scheduled-debug] scheduler_tick_due "
            f"now={now_iso} count={len(tasks)} tasks=[{brief}]"
        )
    for task in tasks:
        tid = task["id"]
        sid = task["session_id"]
        identity_sid = _scheduled_identity_session(sid)
        print(
            "[pupu][scheduled-debug] scheduler_task_start "
            f"task_id={tid} session={sid} run_at={task.get('run_at')} repeat={task.get('repeat_kind')}"
        )
        hint = "这是你自己之前设的定时提醒触发的，自然一点接上就好"
        synthetic = _scheduled_user_message(task)
        try:
            reply = await asyncio.to_thread(
                chat,
                synthetic,
                sid,
                identity_sid == OWNER_SESSION,
                None,
                hint,
                SCHEDULED,
                context_session=sid,
                identity_session=identity_sid,
            )
        except Exception as e:
            print(f"[pupu] scheduled task #{tid} chat failed: {e}")
            print(
                "[pupu][scheduled-debug] scheduler_task_end "
                f"task_id={tid} session={sid} result=chat_failed"
            )
            continue
        if not reply or not str(reply).strip():
            print(
                "[pupu][scheduled-debug] scheduler_task_end "
                f"task_id={tid} session={sid} result=empty_reply"
            )
            continue
        text = str(reply).strip()
        try:
            await send_func(sid, text)
        except Exception as e:
            print(f"[pupu] scheduled task #{tid} send failed: {e}")
            print(
                "[pupu][scheduled-debug] scheduler_task_end "
                f"task_id={tid} session={sid} result=send_failed"
            )
            continue
        _finalize_due_task(task)
        print(
            "[pupu][scheduled-debug] scheduler_task_end "
            f"task_id={tid} session={sid} result=sent_and_finalized"
        )


async def sender_scheduled_tasks_loop(send_func) -> None:
    """Wake periodically and deliver due scheduled tasks through ``send_func``."""
    first_tick = True
    while True:
        if not first_tick:
            await asyncio.sleep(45)
        first_tick = False
        await _run_due_tasks_with_sender(send_func)


def cli_scheduled_tasks_tick() -> None:
    """CLI mode: print due tasks to console (no QQ send)."""
    from .agent import chat

    now_iso = datetime.now().isoformat(timespec="seconds")
    with _scheduler_lock:
        tasks = [
            task
            for task in get_due_scheduled_tasks(now_iso, 10)
            if not _is_wait_followup_task(task)
        ]
    if tasks:
        brief = "; ".join(
            f"id={task['id']} session={task['session_id']} run_at={task['run_at']} repeat={task.get('repeat_kind')} title={str(task.get('title') or '')[:18]}"
            for task in tasks
        )
        print(
            "[pupu][scheduled-debug] cli_tick_due "
            f"now={now_iso} count={len(tasks)} tasks=[{brief}]"
        )
    for task in tasks:
        tid = task["id"]
        sid = task["session_id"]
        identity_sid = _scheduled_identity_session(sid)
        print(
            "[pupu][scheduled-debug] cli_task_start "
            f"task_id={tid} session={sid} run_at={task.get('run_at')} repeat={task.get('repeat_kind')}"
        )

        synthetic = _scheduled_user_message(task)
        hint = "这是你自己之前设的定时提醒触发的，自然一点接上就好"
        source = SCHEDULED

        try:
            reply = chat(
                synthetic,
                sid,
                is_admin=(identity_sid == OWNER_SESSION),
                image_urls=None,
                reply_speed_hint=hint,
                message_source=source,
                context_session=sid,
                identity_session=identity_sid,
            )
        except Exception as e:
            print(f"[pupu] scheduled task #{tid} chat failed: {e}")
            print(
                "[pupu][scheduled-debug] cli_task_end "
                f"task_id={tid} session={sid} result=chat_failed"
            )
            continue
        if not reply or not str(reply).strip():
            print(
                "[pupu][scheduled-debug] cli_task_end "
                f"task_id={tid} session={sid} result=empty_reply"
            )
            continue
        text = str(reply).strip()
        print(f"\n[pupu 定时任务 → {sid}]\n{text}\n")
        _finalize_due_task(task)
        print(
            "[pupu][scheduled-debug] cli_task_end "
            f"task_id={tid} session={sid} result=printed_and_finalized"
        )
