"""Poll and run due scheduled tasks (DB + chat + optional OneBot send)."""

import threading
from datetime import datetime

from .config import load_first_numeric_owner_id
from .memory import finalize_scheduled_task, get_due_scheduled_tasks

_scheduler_lock = threading.Lock()


def _load_first_numeric_owner_qq() -> int | None:
    return load_first_numeric_owner_id()


def _scheduled_user_message(task: dict) -> str:
    title = (task.get("title") or "提醒").strip()
    inst = (task.get("instruction") or "").strip()
    return f"[定时任务「{title}」]\n{inst}"


async def _onebot_send(bot, session_id: str, text: str) -> None:
    """Send scheduled reply to the right peer (NapCat / OneBot v11)."""
    if session_id == "owner":
        u = _load_first_numeric_owner_qq()
        if u is None:
            print("[pupu] scheduled: 未配置数字 owner QQ，无法投递 owner 会话")
            return
        await bot.send_private_msg(user_id=u, message=text)
        return
    if session_id.startswith("private_"):
        tail = session_id[8:]
        if tail.isdigit():
            await bot.send_private_msg(user_id=int(tail), message=text)
        return
    if session_id.startswith("group_"):
        tail = session_id[6:]
        if tail.isdigit():
            await bot.send_group_msg(group_id=int(tail), message=text)
        return
    print(f"[pupu] scheduled: 无法投递会话 {session_id}（仅支持 owner / private_<QQ> / group_<群号>）")


async def onebot_scheduled_tasks_loop(bot) -> None:
    """Wake periodically, run due tasks for this bot's reachable sessions."""
    import asyncio

    from .agent import chat

    while True:
        await asyncio.sleep(45)
        now_iso = datetime.now().isoformat(timespec="seconds")
        with _scheduler_lock:
            tasks = get_due_scheduled_tasks(now_iso, 10)
        for task in tasks:
            tid = task["id"]
            sid = task["session_id"]
            old_run = task["run_at"]
            rk = task.get("repeat_kind") or "once"
            hint = "这是你自己之前设的定时提醒触发的，自然一点接上就好"
            synthetic = _scheduled_user_message(task)
            try:
                reply = await asyncio.to_thread(
                    chat,
                synthetic,
                sid,
                sid == "owner",
                None,
                hint,
                "scheduled",
            )
            except Exception as e:
                print(f"[pupu] scheduled task #{tid} chat failed: {e}")
                continue
            if not reply or not str(reply).strip():
                continue
            text = str(reply).strip()
            try:
                await _onebot_send(bot, sid, text)
            except Exception as e:
                print(f"[pupu] scheduled task #{tid} send failed: {e}")
                continue
            with _scheduler_lock:
                ok = finalize_scheduled_task(
                    tid, old_run, rk, task.get("interval_seconds")
                )
            if not ok:
                print(f"[pupu] scheduled task #{tid}: finalize skipped (stale run_at?)")


def cli_scheduled_tasks_tick() -> None:
    """CLI mode: print due tasks to console (no QQ send)."""
    from .agent import chat

    now_iso = datetime.now().isoformat(timespec="seconds")
    with _scheduler_lock:
        tasks = get_due_scheduled_tasks(now_iso, 10)
    for task in tasks:
        tid = task["id"]
        sid = task["session_id"]
        old_run = task["run_at"]
        rk = task.get("repeat_kind") or "once"
        synthetic = _scheduled_user_message(task)
        hint = "这是你自己之前设的定时提醒触发的，自然一点接上就好"
        try:
            reply = chat(
                synthetic,
                sid,
                is_admin=(sid == "owner"),
                image_urls=None,
                reply_speed_hint=hint,
                message_source="scheduled",
            )
        except Exception as e:
            print(f"[pupu] scheduled task #{tid} chat failed: {e}")
            continue
        if not reply or not str(reply).strip():
            continue
        text = str(reply).strip()
        print(f"\n[pupu 定时任务 → {sid}]\n{text}\n")
        with _scheduler_lock:
            finalize_scheduled_task(tid, old_run, rk, task.get("interval_seconds"))
