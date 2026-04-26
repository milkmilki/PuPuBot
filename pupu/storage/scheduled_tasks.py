"""Persistence helpers for scheduled tasks."""

from __future__ import annotations

import calendar
from datetime import datetime

from .db import get_conn

MAX_SCHEDULED_TASKS_PER_SESSION = 30


def count_scheduled_tasks(session_id: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM scheduled_tasks WHERE session_id = ? AND enabled = 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return int(row["c"]) if row else 0


def create_scheduled_task(
    session_id: str,
    title: str,
    instruction: str,
    run_at: str,
    repeat_kind: str,
    interval_seconds: int | None,
) -> int:
    conn = get_conn()
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO scheduled_tasks
           (session_id, title, instruction, run_at, repeat_kind, interval_seconds, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            session_id,
            title or "提醒",
            instruction,
            run_at,
            repeat_kind,
            interval_seconds,
            now,
        ),
    )
    conn.commit()
    task_id = cursor.lastrowid
    conn.close()
    return int(task_id)


def list_scheduled_tasks(session_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, title, instruction, run_at, repeat_kind, interval_seconds, created_at
           FROM scheduled_tasks
           WHERE session_id = ? AND enabled = 1
           ORDER BY run_at ASC""",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def cancel_scheduled_task(session_id: str, task_id: int) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND session_id = ? AND enabled = 1",
        (task_id, session_id),
    )
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    return changed > 0


def get_due_scheduled_tasks(before_iso: str, limit: int = 10) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, session_id, title, instruction, run_at, repeat_kind, interval_seconds
           FROM scheduled_tasks
           WHERE enabled = 1 AND run_at <= ?
           ORDER BY run_at ASC
           LIMIT ?""",
        (before_iso, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def finalize_scheduled_task(
    task_id: int,
    old_run_at: str,
    repeat_kind: str,
    interval_seconds: int | None,
) -> bool:
    fired_at = datetime.now()
    normalized_repeat = (repeat_kind or "once").lower()
    next_at = None
    if normalized_repeat != "once":
        next_at = _compute_next_run_at_iso(fired_at, normalized_repeat, interval_seconds)
    conn = get_conn()
    if next_at is None:
        cursor = conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = ? AND run_at = ?",
            (task_id, old_run_at),
        )
    else:
        cursor = conn.execute(
            "UPDATE scheduled_tasks SET run_at = ? WHERE id = ? AND run_at = ?",
            (next_at, task_id, old_run_at),
        )
    ok = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def _compute_next_run_at_iso(
    fired_at: datetime,
    repeat_kind: str,
    interval_seconds: int | None,
) -> str | None:
    from datetime import timedelta

    def _add_months(moment: datetime, months: int) -> datetime:
        total_month = (moment.year * 12 + (moment.month - 1)) + months
        year = total_month // 12
        month = total_month % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(moment.day, last_day)
        return moment.replace(year=year, month=month, day=day)

    normalized_repeat = (repeat_kind or "once").lower()
    if normalized_repeat == "once":
        return None
    if normalized_repeat == "daily":
        next_run = fired_at + timedelta(days=1)
    elif normalized_repeat == "weekly":
        next_run = fired_at + timedelta(weeks=1)
    elif normalized_repeat == "monthly":
        next_run = _add_months(fired_at, 1)
    elif normalized_repeat == "yearly":
        next_run = _add_months(fired_at, 12)
    elif normalized_repeat == "interval":
        seconds = int(interval_seconds) if interval_seconds else 3600
        seconds = max(60, min(seconds, 86400 * 7))
        next_run = fired_at + timedelta(seconds=seconds)
    else:
        return None
    return next_run.isoformat(timespec="seconds")
