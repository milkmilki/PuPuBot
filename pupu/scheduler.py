"""Poll and run due scheduled tasks (DB + chat + optional OneBot send)."""

import asyncio
import os
import random
import threading
from datetime import datetime

from .config import load_first_numeric_owner_id
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


def _load_first_numeric_owner_qq() -> int | None:
    return load_first_numeric_owner_id()


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


def _split_message(text: str) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return [text]
    parts = [line.strip() for line in text.split("\n") if line.strip()]
    return parts if parts else [text]


async def _sleep_before_next_segment(next_segment: str) -> None:
    typing_time = min(
        4,
        max(0.8, len(next_segment) * random.uniform(0.05, 0.15)),
    )
    await asyncio.sleep(typing_time)


async def _send_private_segments(bot, user_id: int, segments: list[str]) -> None:
    for index, segment in enumerate(segments):
        await bot.send_private_msg(user_id=user_id, message=segment)
        if index < len(segments) - 1:
            await _sleep_before_next_segment(segments[index + 1])


async def _send_group_segments(bot, group_id: int, segments: list[str]) -> None:
    for index, segment in enumerate(segments):
        await bot.send_group_msg(group_id=group_id, message=segment)
        if index < len(segments) - 1:
            await _sleep_before_next_segment(segments[index + 1])


def _log_scheduled_send(session_label: str, user_label: str, text: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    display = text[:120] + "..." if len(text) > 120 else text
    print(f"[{now}] >>> 发送 | {session_label} | {user_label} | {display}")


async def _onebot_send(bot, session_id: str, text: str) -> None:
    """Send scheduled reply to the right peer (NapCat / OneBot v11)."""
    segments = _split_message(text)
    if session_id == OWNER_SESSION:
        u = _load_first_numeric_owner_qq()
        if u is None:
            print("[pupu] scheduled: 未配置数字 owner QQ，无法投递 owner 会话")
            return
        await _send_private_segments(bot, u, segments)
        _log_scheduled_send("私聊", str(u), text)
        return
    if session_id.startswith("private_"):
        tail = session_id[8:]
        if tail.isdigit():
            await _send_private_segments(bot, int(tail), segments)
            _log_scheduled_send("私聊", tail, text)
        return
    if session_id.startswith("group_"):
        tail = session_id[6:]
        if tail.isdigit():
            await _send_group_segments(bot, int(tail), segments)
            _log_scheduled_send(f"群{tail}", tail, text)
        return
    print(f"[pupu] scheduled: 无法投递会话 {session_id}（仅支持 owner / private_<QQ> / group_<群号>）")


async def onebot_scheduled_tasks_loop(bot) -> None:
    """Wake periodically, run due tasks for this bot's reachable sessions."""
    from .agent import chat

    # First tick runs immediately so reconnects process overdue tasks without waiting
    # an extra poll interval; subsequent ticks wait 45s.
    first_tick = True
    while True:
        if not first_tick:
            await asyncio.sleep(45)
        first_tick = False
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
            source = SCHEDULED

            try:
                reply = await asyncio.to_thread(
                    chat,
                    synthetic,
                    sid,
                    identity_sid == OWNER_SESSION,
                    None,
                    hint,
                    source,
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
                await _onebot_send(bot, sid, text)
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
