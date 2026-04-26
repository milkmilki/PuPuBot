"""Persistence helpers for important conversation events."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .db import get_conn


def derive_source_event_key(
    source_event_key: str | None = None,
    *,
    title: str = "",
    kind: str = "",
    event_time: str = "",
    time_text: str = "",
) -> str:
    raw = str(source_event_key or "").strip().lower()
    if raw:
        normalized = re.sub(r"\s+", "-", raw)
        normalized = normalized[:160].strip("-")
        if normalized:
            return normalized

    base = " | ".join(
        part.strip().lower()
        for part in (title, kind, event_time, time_text)
        if str(part).strip()
    )
    if not base:
        base = datetime.now().isoformat(timespec="seconds")
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]
    return f"event-{digest}"


def upsert_important_events(session_id: str, events: list[dict]) -> list[dict]:
    if not events:
        return []

    conn = get_conn()
    now = datetime.now().isoformat()
    rows = []
    try:
        for event in events:
            source_event_key = derive_source_event_key(
                event.get("source_event_key"),
                title=str(event.get("title", "")),
                kind=str(event.get("kind", "")),
                event_time=str(event.get("event_time", "")),
                time_text=str(event.get("time_text", "")),
            )
            conn.execute(
                """
                INSERT INTO important_events (
                    session_id,
                    source_event_key,
                    title,
                    kind,
                    event_time,
                    time_text,
                    details,
                    followup_hint,
                    confidence,
                    status,
                    linked_task_id,
                    last_seen_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, source_event_key) DO UPDATE SET
                    title = excluded.title,
                    kind = excluded.kind,
                    event_time = excluded.event_time,
                    time_text = excluded.time_text,
                    details = excluded.details,
                    followup_hint = excluded.followup_hint,
                    confidence = excluded.confidence,
                    status = CASE
                        WHEN important_events.linked_task_id IS NOT NULL
                            THEN important_events.status
                        ELSE excluded.status
                    END,
                    linked_task_id = important_events.linked_task_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    session_id,
                    source_event_key,
                    str(event.get("title") or "未命名事件").strip(),
                    str(event.get("kind") or "").strip(),
                    str(event.get("event_time") or "").strip() or None,
                    str(event.get("time_text") or "").strip(),
                    str(event.get("details") or "").strip(),
                    str(event.get("followup_hint") or "").strip(),
                    float(event.get("confidence") or 0.0),
                    str(event.get("status") or "active").strip() or "active",
                    event.get("linked_task_id"),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, session_id, source_event_key, title, kind, event_time, time_text,
                       details, followup_hint, confidence, status, linked_task_id,
                       last_seen_at, created_at
                FROM important_events
                WHERE session_id = ? AND source_event_key = ?
                """,
                (session_id, source_event_key),
            ).fetchone()
            if row:
                rows.append(dict(row))
        conn.commit()
    finally:
        conn.close()
    return rows


def get_important_event_by_key(session_id: str, source_event_key: str) -> dict | None:
    normalized_key = derive_source_event_key(source_event_key)
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT id, session_id, source_event_key, title, kind, event_time, time_text,
                   details, followup_hint, confidence, status, linked_task_id,
                   last_seen_at, created_at
            FROM important_events
            WHERE session_id = ? AND source_event_key = ?
            """,
            (session_id, normalized_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_important_events(
    session_id: str,
    limit: int = 8,
    statuses: tuple[str, ...] = ("active", "scheduled"),
) -> list[dict]:
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT id, session_id, source_event_key, title, kind, event_time, time_text,
                   details, followup_hint, confidence, status, linked_task_id,
                   last_seen_at, created_at
            FROM important_events
            WHERE session_id = ? AND status IN ({placeholders})
            """,
            (session_id, *statuses),
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now()

    def _sort_key(row: dict):
        raw_time = str(row.get("event_time") or "").strip()
        parsed = None
        if raw_time:
            try:
                if len(raw_time) == 10:
                    parsed = datetime.fromisoformat(raw_time + "T23:59:59")
                else:
                    parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        parsed = parsed.astimezone().replace(tzinfo=None)
            except Exception:
                parsed = None

        if parsed is not None and parsed >= now:
            return (0, parsed, -float(row.get("confidence") or 0.0))
        if parsed is None:
            return (1, datetime.max, -float(row.get("confidence") or 0.0))
        return (2, parsed, -float(row.get("confidence") or 0.0))

    items = [dict(row) for row in rows]
    items.sort(key=_sort_key)
    return items[:limit]


def link_important_event_task(
    session_id: str,
    source_event_key: str,
    task_id: int,
    status: str = "scheduled",
) -> None:
    normalized_key = derive_source_event_key(source_event_key)
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE important_events
            SET linked_task_id = ?, status = ?, last_seen_at = ?
            WHERE session_id = ? AND source_event_key = ?
            """,
            (
                int(task_id),
                status,
                datetime.now().isoformat(),
                session_id,
                normalized_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()
