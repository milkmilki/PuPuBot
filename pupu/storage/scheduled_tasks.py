"""Persistence helpers for scheduled tasks."""

from __future__ import annotations

import calendar
import re
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


def cancel_matching_scheduled_tasks(session_id: str, query: str) -> list[dict]:
    query_text = str(query or "").strip()
    if not query_text:
        return []

    rows = list_scheduled_tasks(session_id)
    matches = [row for row in rows if _scheduled_task_matches_query(row, query_text)]
    if not matches:
        return []

    conn = get_conn()
    cancelled = []
    try:
        for row in matches:
            task_id = int(row["id"])
            cursor = conn.execute(
                "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND session_id = ? AND enabled = 1",
                (task_id, session_id),
            )
            if cursor.rowcount <= 0:
                continue
            conn.execute(
                """
                UPDATE important_events
                SET status = 'cancelled', linked_task_id = NULL
                WHERE session_id = ? AND linked_task_id = ?
                """,
                (session_id, task_id),
            )
            cancelled.append(dict(row))
        conn.commit()
    finally:
        conn.close()
    return cancelled


def reschedule_matching_scheduled_tasks(
    session_id: str,
    query: str,
    run_at: str,
    repeat_kind: str | None = None,
    interval_seconds: int | None = None,
) -> list[dict]:
    query_text = str(query or "").strip()
    run_at_text = str(run_at or "").strip()
    if not query_text or not run_at_text:
        return []

    rows = list_scheduled_tasks(session_id)
    matches = [row for row in rows if _scheduled_task_matches_query(row, query_text)]
    if not matches:
        return []

    conn = get_conn()
    updated = []
    try:
        for row in matches:
            task_id = int(row["id"])
            if repeat_kind:
                cursor = conn.execute(
                    """UPDATE scheduled_tasks
                       SET run_at = ?, repeat_kind = ?, interval_seconds = ?
                       WHERE id = ? AND session_id = ? AND enabled = 1""",
                    (
                        run_at_text,
                        repeat_kind,
                        interval_seconds,
                        task_id,
                        session_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """UPDATE scheduled_tasks
                       SET run_at = ?
                       WHERE id = ? AND session_id = ? AND enabled = 1""",
                    (run_at_text, task_id, session_id),
                )
            if cursor.rowcount <= 0:
                continue
            updated_row = dict(row)
            updated_row["old_run_at"] = row.get("run_at")
            updated_row["run_at"] = run_at_text
            if repeat_kind:
                updated_row["repeat_kind"] = repeat_kind
                updated_row["interval_seconds"] = interval_seconds
            updated.append(updated_row)
        conn.commit()
    finally:
        conn.close()
    return updated


def find_matching_scheduled_task(
    session_id: str,
    title: str,
    instruction: str,
    run_at: str,
    repeat_kind: str,
    interval_seconds: int | None,
) -> dict | None:
    conn = get_conn()
    try:
        if interval_seconds is None:
            row = conn.execute(
                """
                SELECT id, title, instruction, run_at, repeat_kind, interval_seconds, created_at
                FROM scheduled_tasks
                WHERE session_id = ?
                  AND enabled = 1
                  AND title = ?
                  AND instruction = ?
                  AND run_at = ?
                  AND repeat_kind = ?
                  AND interval_seconds IS NULL
                LIMIT 1
                """,
                (session_id, title, instruction, run_at, repeat_kind),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, title, instruction, run_at, repeat_kind, interval_seconds, created_at
                FROM scheduled_tasks
                WHERE session_id = ?
                  AND enabled = 1
                  AND title = ?
                  AND instruction = ?
                  AND run_at = ?
                  AND repeat_kind = ?
                  AND interval_seconds = ?
                LIMIT 1
                """,
                (
                    session_id,
                    title,
                    instruction,
                    run_at,
                    repeat_kind,
                    int(interval_seconds),
                ),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def cancel_scheduled_task(session_id: str, task_id: int) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND session_id = ? AND enabled = 1",
        (task_id, session_id),
    )
    if cursor.rowcount > 0:
        conn.execute(
            """
            UPDATE important_events
            SET status = 'cancelled', linked_task_id = NULL
            WHERE session_id = ? AND linked_task_id = ?
            """,
            (session_id, task_id),
        )
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    return changed > 0


def _scheduled_task_matches_query(row: dict, query: str) -> bool:
    haystack = _normalize_match_text(
        " ".join(
            [
                str(row.get("title") or ""),
                str(row.get("instruction") or ""),
                str(row.get("repeat_kind") or ""),
            ]
        )
    )
    needle = _normalize_match_text(query)
    if not needle:
        return False
    if needle in haystack:
        return True

    tokens = _match_tokens(needle)
    if tokens and any(token in haystack for token in tokens):
        return True

    chars = [
        ch
        for ch in needle
        if not ch.isspace() and ch not in "提醒任务定时闹钟待办用户一下一个"
    ]
    if not chars:
        chars = [ch for ch in needle if not ch.isspace()]
    if len(chars) <= 1:
        return needle in haystack
    return sum(1 for ch in set(chars) if ch in haystack) >= min(2, len(set(chars)))


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _match_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", value) if len(token) >= 2]


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
        if cursor.rowcount > 0:
            conn.execute(
                """
                UPDATE important_events
                SET status = 'completed', linked_task_id = NULL
                WHERE linked_task_id = ?
                """,
                (task_id,),
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
