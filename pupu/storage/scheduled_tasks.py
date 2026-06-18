"""Persistence helpers for scheduled tasks."""

from __future__ import annotations

import calendar
import os
import re
from datetime import datetime, timedelta

from ..message_sources import WAIT_FOLLOWUP
from .db import get_conn
from .event_threads import _append_event_step

MAX_SCHEDULED_TASKS_PER_SESSION = 30
SCHEDULED_TASK_GRACE_SECONDS = 3600

_GENERIC_TASK_QUERY_WORDS = {
    "提醒",
    "定时",
    "任务",
    "定时任务",
    "闹钟",
    "待办",
    "那个提醒",
    "这个提醒",
    "那个任务",
    "这个任务",
}


def _debug_enabled() -> bool:
    return str(os.environ.get("PUPU_DEBUG_SCHEDULED_TASKS", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _task_brief(row: dict) -> str:
    return (
        f"id={row.get('id')}"
        f" enabled={row.get('enabled')}"
        f" run_at={row.get('run_at')}"
        f" repeat={row.get('repeat_kind')}"
        f" title={str(row.get('title') or '')[:24]}"
    )


def _debug_dump_session_tasks(conn, session_id: str, tag: str, limit: int = 30) -> None:
    if not _debug_enabled() or not session_id:
        return
    rows = conn.execute(
        """SELECT id, session_id, title, run_at, repeat_kind, enabled
           FROM scheduled_tasks
           WHERE session_id = ?
           ORDER BY id ASC
           LIMIT ?""",
        (session_id, max(1, int(limit))),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM scheduled_tasks WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    enabled = conn.execute(
        "SELECT COUNT(*) AS c FROM scheduled_tasks WHERE session_id = ? AND enabled = 1",
        (session_id,),
    ).fetchone()
    total_count = int(total["c"]) if total else 0
    enabled_count = int(enabled["c"]) if enabled else 0
    preview = "; ".join(_task_brief(dict(row)) for row in rows)
    print(
        "[pupu][scheduled-debug] "
        f"{tag} session={session_id} total={total_count} enabled={enabled_count} preview=[{preview}]"
    )


def _debug_dump_task_by_id(conn, task_id: int, tag: str) -> dict | None:
    if task_id is None:
        return None
    row = conn.execute(
        """SELECT id, session_id, title, instruction, run_at, repeat_kind, interval_seconds, enabled
           FROM scheduled_tasks
           WHERE id = ?""",
        (task_id,),
    ).fetchone()
    data = dict(row) if row else None
    if _debug_enabled():
        print(f"[pupu][scheduled-debug] {tag} task={data}")
    return data


def _mark_event_threads_for_task(conn, task_id: int, *, session_id: str | None, status: str, summary: str, cause: str) -> None:
    now = datetime.now().isoformat()
    if session_id:
        rows = conn.execute(
            "SELECT id FROM event_threads WHERE session_id = ? AND linked_task_id = ?",
            (session_id, int(task_id)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM event_threads WHERE linked_task_id = ?",
            (int(task_id),),
        ).fetchall()
    for row in rows:
        thread_id = int(row["id"])
        conn.execute(
            """UPDATE event_threads
               SET status = ?, linked_task_id = NULL, updated_at = ?
               WHERE id = ?""",
            (status, now, thread_id),
        )
        _append_event_step(
            conn,
            thread_id,
            step_type="system",
            summary=summary,
            cause=cause,
            created_at=now,
        )


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
    if _debug_enabled():
        print(
            "[pupu][scheduled-debug] create request "
            f"session={session_id} title={title} run_at={run_at} repeat={repeat_kind} interval={interval_seconds}"
        )
        _debug_dump_session_tasks(conn, session_id, "create_before")
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
    _debug_dump_task_by_id(conn, int(task_id), "create_after_row")
    _debug_dump_session_tasks(conn, session_id, "create_after")
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
    if _is_query_too_generic(query_text):
        if _debug_enabled():
            print(
                "[pupu][scheduled-debug] cancel_matching rejected_generic "
                f"session={session_id} query={query_text}"
            )
        return []

    rows = list_scheduled_tasks(session_id)
    matches = [row for row in rows if _scheduled_task_matches_query(row, query_text)]
    if _debug_enabled():
        print(
            "[pupu][scheduled-debug] cancel_matching matched "
            f"session={session_id} query={query_text} match_ids={[int(row['id']) for row in matches]}"
        )
    if not matches:
        return []

    conn = get_conn()
    cancelled = []
    try:
        _debug_dump_session_tasks(conn, session_id, "cancel_matching_before")
        for row in matches:
            task_id = int(row["id"])
            cursor = conn.execute(
                "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND session_id = ? AND enabled = 1",
                (task_id, session_id),
            )
            if _debug_enabled():
                print(
                    "[pupu][scheduled-debug] cancel_matching update "
                    f"session={session_id} task_id={task_id} rowcount={cursor.rowcount}"
                )
            if cursor.rowcount <= 0:
                continue
            _mark_event_threads_for_task(
                conn,
                task_id,
                session_id=session_id,
                status="cancelled",
                summary="关联提醒已取消",
                cause="系统取消了匹配的定时任务",
            )
            cancelled.append(dict(row))
        conn.commit()
        _debug_dump_session_tasks(conn, session_id, "cancel_matching_after")
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
    if _is_query_too_generic(query_text):
        if _debug_enabled():
            print(
                "[pupu][scheduled-debug] reschedule_matching rejected_generic "
                f"session={session_id} query={query_text}"
            )
        return []

    rows = list_scheduled_tasks(session_id)
    matches = [row for row in rows if _scheduled_task_matches_query(row, query_text)]
    if _debug_enabled():
        print(
            "[pupu][scheduled-debug] reschedule_matching matched "
            f"session={session_id} query={query_text} match_ids={[int(row['id']) for row in matches]}"
        )
    if not matches:
        return []

    conn = get_conn()
    updated = []
    try:
        _debug_dump_session_tasks(conn, session_id, "reschedule_matching_before")
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
            if _debug_enabled():
                print(
                    "[pupu][scheduled-debug] reschedule_matching update "
                    f"session={session_id} task_id={task_id} rowcount={cursor.rowcount} new_run_at={run_at_text}"
                )
            updated_row = dict(row)
            updated_row["old_run_at"] = row["run_at"]
            updated_row["run_at"] = run_at_text
            if repeat_kind:
                updated_row["repeat_kind"] = repeat_kind
                updated_row["interval_seconds"] = interval_seconds
            updated.append(updated_row)
        conn.commit()
        _debug_dump_session_tasks(conn, session_id, "reschedule_matching_after")
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
    _debug_dump_session_tasks(conn, session_id, "cancel_task_before")
    cursor = conn.execute(
        "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND session_id = ? AND enabled = 1",
        (task_id, session_id),
    )
    if _debug_enabled():
        print(
            "[pupu][scheduled-debug] cancel_task update "
            f"session={session_id} task_id={task_id} rowcount={cursor.rowcount}"
        )
    if cursor.rowcount > 0:
        _mark_event_threads_for_task(
            conn,
            task_id,
            session_id=session_id,
            status="cancelled",
            summary="关联提醒已取消",
            cause="系统取消了指定定时任务",
        )
    conn.commit()
    _debug_dump_task_by_id(conn, task_id, "cancel_task_after_row")
    _debug_dump_session_tasks(conn, session_id, "cancel_task_after")
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


def _is_query_too_generic(query: str) -> bool:
    needle = _normalize_match_text(query)
    if not needle:
        return True
    if needle in _GENERIC_TASK_QUERY_WORDS:
        return True

    # Purely generic characters like “提醒任务” should not cancel/reschedule in bulk.
    specific_chars = [
        ch
        for ch in needle
        if not ch.isspace() and ch not in "提醒任务定时闹钟待办用户一下一个"
    ]
    if not specific_chars and len(needle) <= 4:
        return True
    return False


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _match_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", value) if len(token) >= 2]


def _parse_run_at_iso(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone().replace(tzinfo=None)
        return moment
    except Exception:
        return None


def _compute_next_run_after(
    old_run_at: str,
    repeat_kind: str,
    interval_seconds: int | None,
    after: datetime,
) -> str | None:
    normalized_repeat = (repeat_kind or "once").lower()
    if normalized_repeat == "once":
        return None
    next_run = _parse_run_at_iso(old_run_at)
    if next_run is None:
        next_run = after

    def _advance(moment: datetime) -> datetime | None:
        if normalized_repeat == "daily":
            return moment + timedelta(days=1)
        if normalized_repeat == "weekly":
            return moment + timedelta(weeks=1)
        if normalized_repeat == "monthly":
            return _add_months(moment, 1)
        if normalized_repeat == "yearly":
            return _add_months(moment, 12)
        if normalized_repeat == "interval":
            seconds = int(interval_seconds) if interval_seconds else 3600
            seconds = max(60, min(seconds, 86400 * 7))
            return moment + timedelta(seconds=seconds)
        return None

    for _ in range(5000):
        if next_run > after:
            return next_run.isoformat(timespec="seconds")
        advanced = _advance(next_run)
        if advanced is None:
            return None
        next_run = advanced
    return None


def _skip_missed_scheduled_task(conn, row, now: datetime) -> None:
    task_id = int(row["id"])
    old_run_at = str(row["run_at"])
    repeat_kind = str(row["repeat_kind"] or "once").lower()
    next_at = _compute_next_run_after(
        old_run_at,
        repeat_kind,
        row["interval_seconds"],
        now,
    )
    if _debug_enabled():
        print(
            "[pupu][scheduled-debug] due_missed_skip "
            f"task_id={task_id} session={row['session_id']} run_at={old_run_at} "
            f"repeat={repeat_kind} next_at={next_at}"
        )
    if next_at is None:
        cursor = conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = ? AND run_at = ?",
            (task_id, old_run_at),
        )
        if cursor.rowcount > 0:
            _mark_event_threads_for_task(
                conn,
                task_id,
                session_id=None,
                status="missed",
                summary="关联提醒已错过",
                cause="定时任务超过补触发窗口，被系统跳过",
            )
        return
    conn.execute(
        "UPDATE scheduled_tasks SET run_at = ? WHERE id = ? AND run_at = ?",
        (next_at, task_id, old_run_at),
    )


def _expire_missed_scheduled_tasks(
    conn,
    before: datetime,
    cutoff_iso: str,
    limit: int,
) -> int:
    rows = conn.execute(
        """SELECT id, session_id, title, instruction, run_at, repeat_kind, interval_seconds
           FROM scheduled_tasks
           WHERE enabled = 1 AND run_at < ?
           ORDER BY run_at ASC
           LIMIT ?""",
        (cutoff_iso, max(1, int(limit))),
    ).fetchall()
    for row in rows:
        _skip_missed_scheduled_task(conn, row, before)
    if rows:
        conn.commit()
    return len(rows)


def get_due_scheduled_tasks(before_iso: str, limit: int = 10) -> list[dict]:
    before = _parse_run_at_iso(before_iso) or datetime.now()
    cutoff = before - timedelta(seconds=SCHEDULED_TASK_GRACE_SECONDS)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    conn = get_conn()
    missed_count = _expire_missed_scheduled_tasks(conn, before, cutoff_iso, max(limit * 5, 50))
    raw_rows = conn.execute(
        """SELECT id, session_id, title, instruction, run_at, repeat_kind, interval_seconds
           FROM scheduled_tasks
           WHERE enabled = 1 AND run_at >= ? AND run_at <= ?
           ORDER BY run_at ASC
           LIMIT ?""",
        (cutoff_iso, before.isoformat(timespec="seconds"), limit),
    ).fetchall()
    # sqlite3.Row has no .get(); use subscript. Filter legacy wait_followup titles always.
    rows = [
        row
        for row in raw_rows
        if not str(row["title"] or "").strip().lower().startswith(WAIT_FOLLOWUP)
    ]
    if _debug_enabled() and (raw_rows or missed_count):
        preview = "; ".join(
            f"id={row['id']} session={row['session_id']} run_at={row['run_at']} repeat={row['repeat_kind']} title={str(row['title'])[:18]}"
            for row in raw_rows
        )
        print(
            "[pupu][scheduled-debug] due_fetch "
            f"before={before.isoformat(timespec='seconds')} cutoff={cutoff_iso} "
            f"limit={limit} count={len(raw_rows)} missed={missed_count} tasks=[{preview}]"
        )
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
    before_row = _debug_dump_task_by_id(conn, task_id, "finalize_before_row")
    session_id = str(before_row.get("session_id") or "") if before_row else ""
    if session_id:
        _debug_dump_session_tasks(conn, session_id, "finalize_before")
    if _debug_enabled():
        print(
            "[pupu][scheduled-debug] finalize request "
            f"task_id={task_id} old_run_at={old_run_at} repeat={normalized_repeat} interval={interval_seconds} next_at={next_at}"
        )
    if next_at is None:
        cursor = conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = ? AND run_at = ?",
            (task_id, old_run_at),
        )
        if _debug_enabled():
            print(
                "[pupu][scheduled-debug] finalize delete "
                f"task_id={task_id} rowcount={cursor.rowcount}"
            )
        if cursor.rowcount > 0:
            _mark_event_threads_for_task(
                conn,
                task_id,
                session_id=None,
                status="completed",
                summary="关联提醒已完成",
                cause="定时任务已经触发并完成",
            )
    else:
        cursor = conn.execute(
            "UPDATE scheduled_tasks SET run_at = ? WHERE id = ? AND run_at = ?",
            (next_at, task_id, old_run_at),
        )
        if _debug_enabled():
            print(
                "[pupu][scheduled-debug] finalize update_run_at "
                f"task_id={task_id} next_at={next_at} rowcount={cursor.rowcount}"
            )
    ok = cursor.rowcount > 0
    conn.commit()
    _debug_dump_task_by_id(conn, task_id, "finalize_after_row")
    if session_id:
        _debug_dump_session_tasks(conn, session_id, "finalize_after")
    conn.close()
    return ok


def _compute_next_run_at_iso(
    fired_at: datetime,
    repeat_kind: str,
    interval_seconds: int | None,
) -> str | None:
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


def _add_months(moment: datetime, months: int) -> datetime:
    total_month = (moment.year * 12 + (moment.month - 1)) + months
    year = total_month // 12
    month = total_month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(moment.day, last_day)
    return moment.replace(year=year, month=month, day=day)
