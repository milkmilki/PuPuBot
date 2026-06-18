"""Event-thread persistence and recall."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from .db import get_conn
from .people import (
    INSTANCE_PERSON_KEY,
    OWNER_PERSON_KEY,
    attach_event_people,
    default_instance_person,
    default_owner_person,
    format_people_label,
    get_event_people_for_thread_ids,
    get_people_for_message_range,
    normalize_person_key,
)

RECALL_EVENT_STATUSES = (
    "active",
    "scheduled",
    "completed",
    "done",
    "cancelled",
    "missed",
)
ACTIVE_EVENT_STATUSES = RECALL_EVENT_STATUSES
VALID_STEP_TYPES = {"time", "user", "instance", "system"}
EVENT_THREAD_FTS_TABLE = "event_thread_fts"
FTS_RECALL_LIMIT = 20


def _resolve_identity_session(session_id: str = "default", identity_session: str | None = None) -> str:
    return str(identity_session or session_id or "default")


def derive_thread_key(
    thread_key: str | None = None,
    *,
    title: str = "",
    kind: str = "",
    event_time: str = "",
    time_text: str = "",
) -> str:
    raw = str(thread_key or "").strip().lower()
    if raw:
        normalized = re.sub(r"\s+", "-", raw)
        normalized = re.sub(r"[^0-9a-zA-Z_\-\u4e00-\u9fff]+", "-", normalized)
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


def _text(value: Any, fallback: str = "") -> str:
    return str(value if value is not None else fallback).strip()


def _now() -> str:
    return datetime.now().isoformat()


def _tokens(value: str) -> set[str]:
    text = str(value or "").lower()
    raw_tokens = re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", text)
    out = {token for token in raw_tokens if len(token) >= 2}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        out.update(chunk[i : i + 2] for i in range(max(0, len(chunk) - 1)))
        out.update(chunk[i : i + 3] for i in range(max(0, len(chunk) - 2)))
    return out


def _search_text_from_parts(*parts: Any) -> str:
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def ensure_event_thread_fts(conn) -> bool:
    """Create the optional FTS5 index used for event-thread recall."""
    try:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {EVENT_THREAD_FTS_TABLE}
            USING fts5(
                thread_id UNINDEXED,
                session_id UNINDEXED,
                status UNINDEXED,
                title,
                current_summary,
                followup_hint,
                search_text,
                merge_hint,
                tokenize='trigram'
            )
            """
        )
        return True
    except Exception:
        return False


def _event_thread_fts_available(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = ?",
            (EVENT_THREAD_FTS_TABLE,),
        ).fetchone()
        if row:
            return True
        return ensure_event_thread_fts(conn)
    except Exception:
        return False


def _delete_event_thread_fts(conn, thread_id: int) -> None:
    try:
        if _event_thread_fts_available(conn):
            conn.execute(
                f"DELETE FROM {EVENT_THREAD_FTS_TABLE} WHERE rowid = ?",
                (int(thread_id),),
            )
    except Exception:
        pass


def _refresh_event_thread_fts(conn, thread_id: int) -> None:
    if not _event_thread_fts_available(conn):
        return
    row = conn.execute(
        _thread_select_sql() + " WHERE t.id = ?",
        (int(thread_id),),
    ).fetchone()
    if not row:
        _delete_event_thread_fts(conn, thread_id)
        return
    data = dict(row)
    try:
        conn.execute(
            f"DELETE FROM {EVENT_THREAD_FTS_TABLE} WHERE rowid = ?",
            (int(thread_id),),
        )
        conn.execute(
            f"""INSERT INTO {EVENT_THREAD_FTS_TABLE} (
                   rowid, thread_id, session_id, status, title, current_summary,
                   followup_hint, search_text, merge_hint
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(thread_id),
                int(thread_id),
                data.get("session_id") or "",
                data.get("status") or "",
                data.get("title") or "",
                data.get("current_summary") or "",
                data.get("followup_hint") or "",
                data.get("search_text") or "",
                data.get("merge_hint") or "",
            ),
        )
    except Exception:
        pass


def rebuild_event_thread_fts(conn) -> int:
    """Rebuild the optional FTS index from event_threads."""
    if not ensure_event_thread_fts(conn):
        return 0
    try:
        conn.execute(f"DELETE FROM {EVENT_THREAD_FTS_TABLE}")
        rows = conn.execute("SELECT id FROM event_threads ORDER BY id ASC").fetchall()
        for row in rows:
            _refresh_event_thread_fts(conn, int(row["id"]))
        return len(rows)
    except Exception:
        return 0


def _thread_row_from_db(row: dict[str, Any]) -> dict[str, Any]:
    details = _text(row.get("current_summary")) or _text(row.get("search_text"))
    if _text(row.get("current_cause")):
        details = details or _text(row.get("current_cause"))
    people = row.get("people") if isinstance(row.get("people"), list) else []
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "thread_key": row.get("key"),
        "title": row.get("title") or "未命名事件",
        "kind": row.get("kind") or "",
        "event_time": row.get("event_time"),
        "time_text": row.get("time_text") or "",
        "details": details,
        "followup_hint": row.get("followup_hint") or "",
        "confidence": float(row.get("confidence") or 0.0),
        "status": row.get("status") or "active",
        "linked_task_id": row.get("linked_task_id"),
        "last_seen_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
        "current_step_id": row.get("current_step_id"),
        "current_summary": row.get("current_summary") or "",
        "current_cause": row.get("current_cause") or "",
        "current_reflection": row.get("current_reflection") or "",
        "search_text": row.get("search_text") or "",
        "merge_hint": row.get("merge_hint") or "",
        "origin_person_key": row.get("origin_person_key") or "",
        "people": people,
        "people_label": format_people_label(people),
    }


def _thread_select_sql() -> str:
    return """
        SELECT t.id, t.session_id, t.key, t.title, t.kind, t.status,
               t.current_step_id, t.origin_person_key, t.event_time, t.time_text, t.followup_hint,
               t.confidence, t.linked_task_id, t.search_text, t.merge_hint,
               t.created_at, t.updated_at,
               s.summary AS current_summary,
               s.cause AS current_cause,
               s.reflection AS current_reflection
        FROM event_threads t
        LEFT JOIN event_steps s ON s.id = t.current_step_id
    """


def _fetch_thread_by_key(conn, session_id: str, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        _thread_select_sql() + " WHERE t.session_id = ? AND t.key = ?",
        (session_id, key),
    ).fetchone()
    return dict(row) if row else None


def _fetch_thread_by_id(conn, thread_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        _thread_select_sql() + " WHERE t.id = ?",
        (int(thread_id),),
    ).fetchone()
    return dict(row) if row else None


def _fetch_thread_by_id_in_session(conn, session_id: str, thread_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        _thread_select_sql() + " WHERE t.session_id = ? AND t.id = ?",
        (session_id, int(thread_id)),
    ).fetchone()
    return dict(row) if row else None


def _with_people(conn, row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    data = dict(row)
    data["people"] = get_event_people_for_thread_ids(conn, [int(data["id"])]).get(int(data["id"]), [])
    return data


def _thread_rows_with_people(conn, rows: list[dict[str, Any]] | list[Any]) -> list[dict[str, Any]]:
    raw_rows = [dict(row) for row in rows]
    if not raw_rows:
        return []
    people_by_thread = get_event_people_for_thread_ids(
        conn,
        [int(row["id"]) for row in raw_rows],
    )
    out: list[dict[str, Any]] = []
    for row in raw_rows:
        row["people"] = people_by_thread.get(int(row["id"]), [])
        out.append(_thread_row_from_db(row))
    return out


def _default_event_people() -> list[dict[str, Any]]:
    return [default_owner_person(), default_instance_person()]


def _event_range_people(conn, session_id: str, event: dict[str, Any]) -> list[dict[str, Any]]:
    start_id = event.get("source_msg_start_id")
    end_id = event.get("source_msg_end_id")
    if start_id is None or end_id is None:
        return _default_event_people()
    context_session = _text(event.get("source_context_session")) or session_id
    return get_people_for_message_range(conn, context_session, start_id, end_id)


def _origin_person_key(people: list[dict[str, Any]]) -> str:
    for person in people or []:
        key = normalize_person_key(person.get("person_key") if isinstance(person, dict) else "")
        if key and key != INSTANCE_PERSON_KEY:
            return key
    return OWNER_PERSON_KEY


def _rebuild_thread_search_text(conn, thread_id: int) -> str:
    thread = conn.execute(
        """SELECT title, kind, event_time, time_text, followup_hint, merge_hint
           FROM event_threads WHERE id = ?""",
        (int(thread_id),),
    ).fetchone()
    if not thread:
        return ""
    steps = conn.execute(
        """SELECT summary, cause, reflection
           FROM event_steps
           WHERE thread_id = ?
           ORDER BY id DESC
           LIMIT 4""",
        (int(thread_id),),
    ).fetchall()
    search_text = _search_text_from_parts(
        thread["title"],
        thread["kind"],
        thread["event_time"],
        thread["time_text"],
        thread["followup_hint"],
        thread["merge_hint"],
        *(
            _search_text_from_parts(step["summary"], step["cause"], step["reflection"])
            for step in steps
        ),
    )
    conn.execute(
        "UPDATE event_threads SET search_text = ? WHERE id = ?",
        (search_text, int(thread_id)),
    )
    _refresh_event_thread_fts(conn, int(thread_id))
    return search_text


def _append_event_step(
    conn,
    thread_id: int,
    *,
    step_type: str,
    summary: str,
    cause: str = "",
    reflection: str = "",
    occurred_at: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
    created_at: str | None = None,
    people: list[dict[str, Any]] | None = None,
    origin_person_key: str | None = None,
    people_source: str = "inferred",
) -> int:
    created_at = created_at or _now()
    normalized_type = str(step_type or "user").strip().lower()
    if normalized_type not in VALID_STEP_TYPES:
        normalized_type = "user"
    summary_text = _text(summary)
    if normalized_type == "time" and summary_text and not any(
        marker in summary_text for marker in ("可能", "推测", "大概", "也许")
    ):
        summary_text = "推测：" + summary_text
    cursor = conn.execute(
        """INSERT INTO event_steps (
               thread_id, step_type, summary, cause, reflection, occurred_at,
               source_msg_start_id, source_msg_end_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(thread_id),
            normalized_type,
            summary_text,
            _text(cause),
            _text(reflection),
            _text(occurred_at) or None,
            source_msg_start_id,
            source_msg_end_id,
            created_at,
        ),
    )
    step_id = int(cursor.lastrowid)
    conn.execute(
        "UPDATE event_threads SET current_step_id = ?, updated_at = ? WHERE id = ?",
        (step_id, created_at, int(thread_id)),
    )
    if people is not None:
        attach_event_people(
            conn,
            int(thread_id),
            people,
            origin_person_key=origin_person_key,
            source=people_source,
            now=created_at,
        )
        attach_event_people(
            conn,
            int(thread_id),
            people,
            step_id=step_id,
            origin_person_key=origin_person_key,
            source=people_source,
            now=created_at,
        )
    _rebuild_thread_search_text(conn, int(thread_id))
    return step_id


def append_event_step(
    session_id: str,
    key: str,
    *,
    step_type: str,
    summary: str,
    cause: str = "",
    reflection: str = "",
    occurred_at: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
    identity_session: str | None = None,
) -> dict[str, Any] | None:
    session_id = _resolve_identity_session(session_id, identity_session)
    normalized_key = derive_thread_key(key)
    conn = get_conn()
    try:
        thread = _fetch_thread_by_key(conn, session_id, normalized_key)
        if not thread:
            return None
        _append_event_step(
            conn,
            int(thread["id"]),
            step_type=step_type,
            summary=summary,
            cause=cause,
            reflection=reflection,
            occurred_at=occurred_at,
            source_msg_start_id=source_msg_start_id,
            source_msg_end_id=source_msg_end_id,
            people=get_people_for_message_range(
                conn,
                session_id,
                source_msg_start_id,
                source_msg_end_id,
            )
            if source_msg_start_id is not None and source_msg_end_id is not None
            else _default_event_people(),
            origin_person_key=thread.get("origin_person_key") or OWNER_PERSON_KEY,
        )
        conn.commit()
        updated = _fetch_thread_by_key(conn, session_id, normalized_key)
        updated = _with_people(conn, updated)
        return _thread_row_from_db(updated) if updated else None
    finally:
        conn.close()


def _upsert_thread_from_event(conn, session_id: str, event: dict[str, Any], now: str) -> dict[str, Any] | None:
    title = _text(event.get("title"), "未命名事件")
    kind = _text(event.get("kind")).lower()
    event_time = _text(event.get("event_time")) or None
    time_text = _text(event.get("time_text"))
    details = _text(event.get("details"))
    followup_hint = _text(event.get("followup_hint"))
    merge_hint = _text(event.get("merge_hint")) or followup_hint
    try:
        confidence = max(0.0, min(1.0, float(event.get("confidence") or 0.0)))
    except Exception:
        confidence = 0.0
    status = _text(event.get("status"), "active") or "active"
    key = derive_thread_key(
        event.get("thread_key") or event.get("key"),
        title=title,
        kind=kind,
        event_time=event_time or "",
        time_text=time_text,
    )
    event_people = _event_range_people(conn, session_id, event)
    origin_person_key = _origin_person_key(event_people)
    row = _fetch_thread_by_key(conn, session_id, key)
    if row is None:
        search_text = _search_text_from_parts(title, kind, event_time, time_text, details, followup_hint, merge_hint)
        cursor = conn.execute(
            """INSERT INTO event_threads (
                   session_id, key, title, kind, status, event_time, time_text,
                   followup_hint, confidence, linked_task_id, search_text,
                   origin_person_key,
                   merge_hint, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                key,
                title,
                kind,
                status,
                event_time,
                time_text,
                followup_hint,
                confidence,
                event.get("linked_task_id"),
                search_text,
                origin_person_key,
                merge_hint,
                now,
                now,
            ),
        )
        thread_id = int(cursor.lastrowid)
        _append_event_step(
            conn,
            thread_id,
            step_type=_text(event.get("step_type"), "user"),
            summary=details or title,
            cause=_text(event.get("cause")) or "事件被首次记录",
            reflection=_text(event.get("reflection")),
            occurred_at=event_time,
            source_msg_start_id=event.get("source_msg_start_id"),
            source_msg_end_id=event.get("source_msg_end_id"),
            created_at=now,
            people=event_people,
            origin_person_key=origin_person_key,
            people_source="message_range",
        )
        return _with_people(conn, _fetch_thread_by_id(conn, thread_id))

    thread_id = int(row["id"])
    conn.execute(
        """UPDATE event_threads
           SET title = ?, kind = ?, event_time = ?, time_text = ?, followup_hint = ?,
               confidence = ?, status = CASE
                   WHEN linked_task_id IS NOT NULL THEN status
                   ELSE ?
               END,
               linked_task_id = linked_task_id,
               merge_hint = ?,
               updated_at = ?
           WHERE id = ?""",
        (
            title,
            kind,
            event_time,
            time_text,
            followup_hint,
            confidence,
            status,
            merge_hint,
            now,
            thread_id,
        ),
    )
    current_summary = _text(row.get("current_summary"))
    if details and details != current_summary:
        _append_event_step(
            conn,
            thread_id,
            step_type=_text(event.get("step_type"), "user"),
            summary=details,
            cause=_text(event.get("cause")) or "事件线收到新的进展",
            reflection=_text(event.get("reflection")),
            occurred_at=event_time,
            source_msg_start_id=event.get("source_msg_start_id"),
            source_msg_end_id=event.get("source_msg_end_id"),
            created_at=now,
            people=event_people,
            origin_person_key=row.get("origin_person_key") or origin_person_key,
            people_source="message_range",
        )
    else:
        _rebuild_thread_search_text(conn, thread_id)
    return _with_people(conn, _fetch_thread_by_id(conn, thread_id))


def _event_text_for_match(event: dict[str, Any]) -> str:
    return _search_text_from_parts(
        event.get("title"),
        event.get("kind"),
        event.get("event_time"),
        event.get("time_text"),
        event.get("details"),
        event.get("followup_hint"),
        event.get("summary"),
        event.get("cause"),
    )


def _best_related_thread(conn, session_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
    explicit_key = _text(event.get("thread_key") or event.get("existing_thread_key"))
    if explicit_key:
        row = _fetch_thread_by_key(conn, session_id, derive_thread_key(explicit_key))
        if row:
            return row
    candidates = find_related_event_threads(
        session_id,
        _event_text_for_match(event),
        limit=1,
        person_keys={person.get("person_key") for person in _event_range_people(conn, session_id, event)},
        _conn=conn,
    )
    if candidates and float(candidates[0].get("score") or 0.0) >= 0.32:
        return candidates[0]
    return None


def upsert_event_threads(
    session_id: str,
    events: list[dict],
    *,
    identity_session: str | None = None,
) -> list[dict]:
    """Write event updates into event_threads/event_steps."""
    session_id = _resolve_identity_session(session_id, identity_session)
    if not events:
        return []

    conn = get_conn()
    now = _now()
    rows: list[dict] = []
    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            action = _text(event.get("action") or "upsert").lower()
            if action == "append_step":
                target = _best_related_thread(conn, session_id, event)
                if not target:
                    target = _upsert_thread_from_event(conn, session_id, event, now)
                if target:
                    summary = _text(event.get("summary") or event.get("details") or target.get("title"))
                    if summary:
                        event_people = _event_range_people(conn, session_id, event)
                        _append_event_step(
                            conn,
                            int(target["id"]),
                            step_type=_text(event.get("step_type"), "user"),
                            summary=summary,
                            cause=_text(event.get("cause")) or "事件线收到新的进展",
                            reflection=_text(event.get("reflection")),
                            occurred_at=_text(event.get("occurred_at") or event.get("event_time")) or None,
                            source_msg_start_id=event.get("source_msg_start_id"),
                            source_msg_end_id=event.get("source_msg_end_id"),
                            created_at=now,
                            people=event_people,
                            origin_person_key=target.get("origin_person_key") or _origin_person_key(event_people),
                            people_source="message_range",
                        )
                    updated = _with_people(conn, _fetch_thread_by_id(conn, int(target["id"])))
                    if updated:
                        rows.append(_thread_row_from_db(updated))
                continue

            target = None
            if not _text(event.get("thread_key") or event.get("key")):
                target = _best_related_thread(conn, session_id, event)
            if target is not None:
                target_key = target.get("key") or target.get("thread_key")
                event = {**event, "thread_key": target_key, "action": "append_step"}
                summary = _text(event.get("summary") or event.get("details") or event.get("title"))
                if summary:
                    event_people = _event_range_people(conn, session_id, event)
                    _append_event_step(
                        conn,
                        int(target["id"]),
                        step_type=_text(event.get("step_type"), "user"),
                        summary=summary,
                        cause=_text(event.get("cause")) or "相似事件被归并到已有事件线",
                        reflection=_text(event.get("reflection")),
                        occurred_at=_text(event.get("occurred_at") or event.get("event_time")) or None,
                        source_msg_start_id=event.get("source_msg_start_id"),
                        source_msg_end_id=event.get("source_msg_end_id"),
                        created_at=now,
                        people=event_people,
                        origin_person_key=target.get("origin_person_key") or _origin_person_key(event_people),
                        people_source="message_range",
                    )
                updated = _with_people(conn, _fetch_thread_by_id(conn, int(target["id"])))
            else:
                updated = _upsert_thread_from_event(conn, session_id, event, now)
            if updated:
                rows.append(_thread_row_from_db(updated))
        conn.commit()
    finally:
        conn.close()
    return rows


def get_event_thread_by_key(
    session_id: str,
    thread_key: str,
    *,
    identity_session: str | None = None,
) -> dict | None:
    session_id = _resolve_identity_session(session_id, identity_session)
    normalized_key = derive_thread_key(thread_key)
    conn = get_conn()
    try:
        row = _fetch_thread_by_key(conn, session_id, normalized_key)
        row = _with_people(conn, row)
        return _thread_row_from_db(row) if row else None
    finally:
        conn.close()


def _status_placeholders(statuses: tuple[str, ...]) -> str:
    return ",".join("?" for _ in statuses)


def _parse_event_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.fromisoformat(raw + "T23:59:59")
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def get_event_threads(
    session_id: str,
    limit: int = 8,
    statuses: tuple[str, ...] = ACTIVE_EVENT_STATUSES,
    *,
    identity_session: str | None = None,
) -> list[dict]:
    session_id = _resolve_identity_session(session_id, identity_session)
    conn = get_conn()
    try:
        placeholders = _status_placeholders(statuses)
        rows = conn.execute(
            _thread_select_sql()
            + f" WHERE t.session_id = ? AND t.status IN ({placeholders})",
            (session_id, *statuses),
        ).fetchall()
        now = datetime.now()
        items = _thread_rows_with_people(conn, rows)

        def _sort_key(row: dict):
            parsed = _parse_event_time(str(row.get("event_time") or ""))
            if parsed is not None and parsed >= now:
                return (0, parsed, -float(row.get("confidence") or 0.0))
            if parsed is None:
                return (1, datetime.max, -float(row.get("confidence") or 0.0))
            return (2, parsed, -float(row.get("confidence") or 0.0))

        items.sort(key=_sort_key)
        return items[:limit]
    finally:
        conn.close()


def get_recent_event_threads(
    session_id: str,
    limit: int | None = None,
    statuses: tuple[str, ...] = ACTIVE_EVENT_STATUSES,
    *,
    identity_session: str | None = None,
) -> list[dict]:
    """Return report-ordered event threads; ``limit=None`` returns all."""
    session_id = _resolve_identity_session(session_id, identity_session)
    conn = get_conn()
    try:
        placeholders = _status_placeholders(statuses)
        sql = (
            _thread_select_sql()
            + f"""
              WHERE t.session_id = ? AND t.status IN ({placeholders})
              ORDER BY t.updated_at DESC, t.created_at DESC, t.id DESC
            """
        )
        params: tuple[object, ...] = (session_id, *statuses)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, int(limit))
        rows = conn.execute(sql, params).fetchall()
        return _thread_rows_with_people(conn, rows)
    finally:
        conn.close()


def get_recent_event_threads_from_conn(
    conn,
    session_id: str,
    limit: int | None = None,
    statuses: tuple[str, ...] = ACTIVE_EVENT_STATUSES,
) -> list[dict[str, Any]]:
    """Connection-scoped variant for maintenance snapshots."""
    session_id = _resolve_identity_session(session_id)
    placeholders = _status_placeholders(statuses)
    sql = (
        _thread_select_sql()
        + f"""
          WHERE t.session_id = ? AND t.status IN ({placeholders})
          ORDER BY t.updated_at DESC, t.created_at DESC, t.id DESC
        """
    )
    params: tuple[object, ...] = (session_id, *statuses)
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, int(limit))
    rows = conn.execute(sql, params).fetchall()
    return _thread_rows_with_people(conn, rows)


def get_event_thread_steps(
    session_id: str,
    key: str,
    *,
    identity_session: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    session_id = _resolve_identity_session(session_id, identity_session)
    normalized_key = derive_thread_key(key)
    conn = get_conn()
    try:
        thread = _fetch_thread_by_key(conn, session_id, normalized_key)
        if not thread:
            return None, []
        rows = conn.execute(
            """SELECT id, thread_id, step_type, summary, cause, reflection,
                      occurred_at, source_msg_start_id, source_msg_end_id, created_at
               FROM event_steps
               WHERE thread_id = ?
               ORDER BY id ASC""",
            (int(thread["id"]),),
        ).fetchall()
        thread = _with_people(conn, thread)
        people_by_thread = get_event_people_for_thread_ids(conn, [int(thread["id"])]) if thread else {}
        step_rows = []
        for row in rows:
            item = dict(row)
            item["people"] = [
                person
                for person in people_by_thread.get(int(item["thread_id"]), [])
                if person.get("step_id") == item["id"]
            ]
            item["people_label"] = format_people_label(item["people"])
            step_rows.append(item)
        return _thread_row_from_db(thread), step_rows
    finally:
        conn.close()


def get_event_thread_recent_steps(
    session_id: str,
    key: str,
    *,
    limit: int = 3,
    identity_session: str | None = None,
    _conn=None,
) -> list[dict[str, Any]]:
    session_id = _resolve_identity_session(session_id, identity_session)
    normalized_key = derive_thread_key(key)
    conn = _conn or get_conn()
    close_conn = _conn is None
    try:
        thread = _fetch_thread_by_key(conn, session_id, normalized_key)
        if not thread:
            return []
        rows = conn.execute(
            """SELECT id, thread_id, step_type, summary, cause, reflection,
                      occurred_at, source_msg_start_id, source_msg_end_id, created_at
               FROM event_steps
               WHERE thread_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (int(thread["id"]), max(1, int(limit))),
        ).fetchall()
        return list(reversed([dict(row) for row in rows]))
    finally:
        if close_conn:
            conn.close()


def _fts_phrase(value: str) -> str:
    return '"' + str(value or "").replace('"', '""') + '"'


def _fts_query(text: str) -> str:
    terms: list[str] = []
    for token in sorted(_tokens(text), key=len, reverse=True):
        if len(token) < 3:
            continue
        if re.search(r"[\u4e00-\u9fff]", token):
            terms.append(_fts_phrase(token))
        elif re.fullmatch(r"[0-9a-zA-Z_][0-9a-zA-Z_\-]*", token):
            terms.append(_fts_phrase(token))
        if len(terms) >= 12:
            break
    if terms:
        return " OR ".join(terms)
    query = _text(text)
    return _fts_phrase(query) if len(query) >= 3 else ""


def _fetch_event_thread_candidates(
    conn,
    session_id: str,
    statuses: tuple[str, ...],
    *,
    thread_ids: list[int] | None = None,
):
    placeholders = _status_placeholders(statuses)
    params: list[Any] = [session_id, *statuses]
    where = f" WHERE t.session_id = ? AND t.status IN ({placeholders})"
    if thread_ids is not None:
        if not thread_ids:
            return []
        id_placeholders = ",".join("?" for _ in thread_ids)
        where += f" AND t.id IN ({id_placeholders})"
        params.extend(int(item) for item in thread_ids)
    return conn.execute(_thread_select_sql() + where, tuple(params)).fetchall()


def _fts_candidate_scores(
    conn,
    session_id: str,
    query_text: str,
    statuses: tuple[str, ...],
    *,
    limit: int,
) -> tuple[dict[int, float], bool]:
    if not _event_thread_fts_available(conn):
        return {}, False
    match_query = _fts_query(query_text)
    if not match_query:
        return {}, True
    try:
        placeholders = _status_placeholders(statuses)
        rows = conn.execute(
            f"""SELECT thread_id, bm25({EVENT_THREAD_FTS_TABLE}) AS rank
                FROM {EVENT_THREAD_FTS_TABLE}
                WHERE {EVENT_THREAD_FTS_TABLE} MATCH ?
                  AND session_id = ?
                  AND status IN ({placeholders})
                ORDER BY rank ASC
                LIMIT ?""",
            (match_query, session_id, *statuses, max(1, int(limit))),
        ).fetchall()
    except Exception:
        return {}, False
    scores: dict[int, float] = {}
    ranks = [(int(row["thread_id"]), float(row["rank"] or 0.0)) for row in rows]
    if not ranks:
        return {}, True
    best = min(rank for _, rank in ranks)
    worst = max(rank for _, rank in ranks)
    span = max(0.000001, worst - best)
    for thread_id, rank in ranks:
        normalized = 1.0 - ((rank - best) / span) if len(ranks) > 1 else 1.0
        scores[thread_id] = max(0.0, min(1.0, normalized))
    return scores, True


def _score_event_thread(
    row: dict[str, Any],
    query_tokens: set[str],
    *,
    now: datetime,
    fts_score: float | None = None,
    person_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    scored_row = _thread_row_from_db(row)
    haystack = _search_text_from_parts(
        scored_row.get("title"),
        scored_row.get("details"),
        scored_row.get("followup_hint"),
        scored_row.get("search_text"),
        scored_row.get("merge_hint"),
    )
    hay_tokens = _tokens(haystack)
    overlap_tokens = sorted(query_tokens & hay_tokens)
    overlap = len(overlap_tokens)
    if overlap <= 0 and fts_score is None:
        return None

    overlap_score = overlap / max(1, min(len(query_tokens), len(hay_tokens)))
    fts_component = max(0.0, min(1.0, float(fts_score))) if fts_score is not None else 0.0
    status_bonus = 0.12 if scored_row.get("status") in ACTIVE_EVENT_STATUSES else 0.0
    confidence_bonus = min(0.08, max(0.0, float(scored_row.get("confidence") or 0.0)) * 0.08)
    query_people = {normalize_person_key(item) for item in (person_keys or set()) if normalize_person_key(item)}
    event_people = {
        normalize_person_key(person.get("person_key"))
        for person in scored_row.get("people") or []
        if normalize_person_key(person.get("person_key"))
    }
    matched_people = sorted(query_people & event_people)
    if query_people and matched_people:
        people_bonus = 0.26
    elif query_people and event_people:
        people_bonus = -0.18
    else:
        people_bonus = 0.0
    recent_bonus = 0.0
    try:
        updated = datetime.fromisoformat(str(scored_row.get("last_seen_at") or ""))
        age_days = max(0.0, (now - updated).total_seconds() / 86400)
        recent_bonus = max(0.0, 0.18 - min(age_days, 30) * 0.006)
    except Exception:
        pass

    raw_score = (
        (fts_component * 0.28)
        + (overlap_score * 0.54)
        + status_bonus
        + recent_bonus
        + confidence_bonus
        + people_bonus
    )
    score = max(0.0, min(1.0, raw_score))
    scored_row["score"] = score
    scored_row["match_debug"] = {
        "fts_score": fts_component,
        "overlap_score": overlap_score,
        "overlap_tokens": overlap_tokens[:12],
        "status_bonus": status_bonus,
        "recent_bonus": recent_bonus,
        "confidence_bonus": confidence_bonus,
        "people_bonus": people_bonus,
        "query_people": sorted(query_people),
        "event_people": sorted(event_people),
        "matched_people": matched_people,
        "total": score,
    }
    reason_bits = []
    if fts_score is not None:
        reason_bits.append(f"fts={fts_component:.2f}")
    if overlap_tokens:
        reason_bits.append("matched: " + ", ".join(overlap_tokens[:8]))
    if status_bonus:
        reason_bits.append(f"status+{status_bonus:.2f}")
    if recent_bonus:
        reason_bits.append(f"recent+{recent_bonus:.2f}")
    if confidence_bonus:
        reason_bits.append(f"confidence+{confidence_bonus:.2f}")
    if people_bonus:
        reason_bits.append(f"people{people_bonus:+.2f}")
    scored_row["reason_for_match"] = "; ".join(reason_bits) or "fts candidate"
    return scored_row


def find_related_event_threads(
    session_id: str,
    text: str,
    limit: int = 5,
    *,
    statuses: tuple[str, ...] = ACTIVE_EVENT_STATUSES,
    debug: bool = False,
    person_keys: set[str] | list[str] | tuple[str, ...] | None = None,
    _conn=None,
) -> list[dict[str, Any]]:
    session_id = _resolve_identity_session(session_id)
    query_text = _text(text)
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return []

    conn = _conn or get_conn()
    close_conn = _conn is None
    try:
        fts_scores, fts_attempted = _fts_candidate_scores(
            conn,
            session_id,
            query_text,
            statuses,
            limit=max(FTS_RECALL_LIMIT, int(limit) * 3),
        )
        if fts_scores:
            rows = _fetch_event_thread_candidates(
                conn,
                session_id,
                statuses,
                thread_ids=list(fts_scores.keys()),
            )
        else:
            rows = _fetch_event_thread_candidates(conn, session_id, statuses)
        raw_rows = [dict(row) for row in rows]
        people_by_thread = get_event_people_for_thread_ids(
            conn,
            [int(row["id"]) for row in raw_rows],
        )
    finally:
        if close_conn:
            conn.close()

    now = datetime.now()
    scored: list[dict[str, Any]] = []
    normalized_people = {
        normalize_person_key(item) for item in (person_keys or []) if normalize_person_key(item)
    }
    for row in raw_rows:
        row["people"] = people_by_thread.get(int(row["id"]), [])
        fts_score = fts_scores.get(int(row["id"]))
        scored_row = _score_event_thread(
            row,
            query_tokens,
            now=now,
            fts_score=fts_score,
            person_keys=normalized_people,
        )
        if scored_row:
            if debug:
                scored_row["match_debug"]["fts_attempted"] = bool(fts_attempted)
                scored_row["match_debug"]["used_fts_candidate"] = fts_score is not None
            scored.append(scored_row)

    scored.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            str(item.get("last_seen_at") or ""),
        ),
        reverse=True,
    )
    return scored[: max(1, int(limit))]


def link_event_thread_task(
    session_id: str,
    thread_key: str,
    task_id: int,
    status: str = "scheduled",
    *,
    identity_session: str | None = None,
) -> None:
    session_id = _resolve_identity_session(session_id, identity_session)
    normalized_key = derive_thread_key(thread_key)
    conn = get_conn()
    now = _now()
    try:
        row = _fetch_thread_by_key(conn, session_id, normalized_key)
        if row:
            conn.execute(
                """UPDATE event_threads
                   SET linked_task_id = ?, status = ?, updated_at = ?
                   WHERE session_id = ? AND key = ?""",
                (int(task_id), status, now, session_id, normalized_key),
            )
            _append_event_step(
                conn,
                int(row["id"]),
                step_type="system",
                summary=f"已关联定时任务 #{int(task_id)}",
                cause="系统根据事件创建或更新提醒任务",
                created_at=now,
            )
        conn.commit()
    finally:
        conn.close()


def update_event_threads_for_task(
    session_id: str | None,
    task_id: int,
    *,
    status: str,
    summary: str,
    cause: str = "",
) -> int:
    conn = get_conn()
    now = _now()
    changed = 0
    try:
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
            conn.execute(
                """UPDATE event_threads
                   SET status = ?, linked_task_id = NULL, updated_at = ?
                   WHERE id = ?""",
                (status, now, int(row["id"])),
            )
            _append_event_step(
                conn,
                int(row["id"]),
                step_type="system",
                summary=summary,
                cause=cause,
                created_at=now,
            )
            changed += 1
        conn.commit()
    finally:
        conn.close()
    return changed


def apply_event_thread_maintenance(
    conn,
    session_id: str,
    *,
    updates: list[dict[str, Any]] | None = None,
    now: str | None = None,
) -> int:
    """Apply non-destructive model-maintenance updates to event_threads."""
    session_id = _resolve_identity_session(session_id)
    now = now or _now()
    updated = 0

    for update in updates or []:
        row = _fetch_thread_by_id_in_session(conn, session_id, int(update.get("id") or 0))
        if not row:
            continue
        title = _text(update.get("title")) or row.get("title") or "未命名事件"
        kind = _text(update.get("kind")) or row.get("kind") or ""
        event_time = _text(update.get("event_time")) or row.get("event_time")
        time_text = _text(update.get("time_text")) or row.get("time_text") or ""
        details = _text(update.get("details"))
        followup_hint = _text(update.get("followup_hint")) or row.get("followup_hint") or ""
        try:
            confidence = max(0.0, min(1.0, float(update.get("confidence", row.get("confidence") or 0.0))))
        except Exception:
            confidence = float(row.get("confidence") or 0.0)
        conn.execute(
            """UPDATE event_threads
               SET title = ?, kind = ?, event_time = ?, time_text = ?,
                   followup_hint = ?, confidence = ?, merge_hint = ?, updated_at = ?
               WHERE id = ?""",
            (
                title,
                kind,
                event_time,
                time_text,
                followup_hint,
                confidence,
                followup_hint,
                now,
                int(row["id"]),
            ),
        )
        current_summary = _text(row.get("current_summary"))
        if details and details != current_summary:
            _append_event_step(
                conn,
                int(row["id"]),
                step_type="system",
                summary=details,
                cause="模型维护合并或重写了事件线当前状态",
                created_at=now,
            )
        else:
            _rebuild_thread_search_text(conn, int(row["id"]))
        updated += 1
    return updated


def event_graph_payload(session_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        threads = conn.execute(
            _thread_select_sql()
            + " WHERE t.session_id = ? ORDER BY t.updated_at DESC, t.id DESC",
            (session_id,),
        ).fetchall()
        thread_rows = [_thread_row_from_db(dict(row)) for row in threads]
        thread_ids = [int(row["id"]) for row in thread_rows]
        people_by_thread = get_event_people_for_thread_ids(conn, thread_ids)
        for thread in thread_rows:
            thread["people"] = people_by_thread.get(int(thread["id"]), [])
            thread["people_label"] = format_people_label(thread["people"])
        people_by_key: dict[str, dict[str, Any]] = {}
        for people in people_by_thread.values():
            for person in people:
                key = normalize_person_key(person.get("person_key"))
                if key and key not in people_by_key:
                    people_by_key[key] = dict(person)
        steps: list[dict[str, Any]] = []
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            step_rows = conn.execute(
                f"""SELECT id, thread_id, step_type, summary, cause, reflection,
                           occurred_at, source_msg_start_id, source_msg_end_id, created_at
                    FROM event_steps
                    WHERE thread_id IN ({placeholders})
                    ORDER BY thread_id ASC, id ASC""",
                tuple(thread_ids),
            ).fetchall()
            steps = [dict(row) for row in step_rows]
            for step in steps:
                step_people = [
                    person
                    for person in people_by_thread.get(int(step["thread_id"]), [])
                    if person.get("step_id") == step["id"]
                ]
                step["people"] = step_people
                step["people_label"] = format_people_label(step_people)
    finally:
        conn.close()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for person in people_by_key.values():
        person_key = normalize_person_key(person.get("person_key"))
        if not person_key:
            continue
        nodes.append(
            {
                "id": f"person-{person_key}",
                "type": "person",
                "person_key": person_key,
                "label": person.get("display_name") or person_key,
                "kind": person.get("kind") or "",
            }
        )
    for thread in thread_rows:
        nodes.append(
            {
                "id": f"thread-{thread['id']}",
                "type": "thread",
                "thread_id": thread["id"],
                "key": thread["thread_key"],
                "label": thread["title"],
                "status": thread["status"],
                "summary": thread.get("current_summary") or thread.get("details") or "",
                "people": thread.get("people") or [],
                "people_label": thread.get("people_label") or "",
            }
        )
        people_edge_roles: dict[str, list[str]] = {}
        for person in thread.get("people") or []:
            person_key = normalize_person_key(person.get("person_key"))
            if not person_key or person.get("step_id") is not None:
                continue
            role = _text(person.get("role")) or "participant"
            roles = people_edge_roles.setdefault(person_key, [])
            if role not in roles:
                roles.append(role)
        for person_key, roles in people_edge_roles.items():
            edges.append(
                {
                    "id": f"edge-person-{person_key}-thread-{thread['id']}",
                    "source": f"person-{person_key}",
                    "target": f"thread-{thread['id']}",
                    "label": "/".join(roles),
                    "type": "person_thread",
                }
            )
    previous_by_thread: dict[int, str] = {}
    for step in steps:
        node_id = f"step-{step['id']}"
        thread_id = int(step["thread_id"])
        nodes.append(
            {
                "id": node_id,
                "type": "step",
                "thread_id": thread_id,
                "label": step["summary"],
                "step_type": step["step_type"],
                "cause": step["cause"],
                "reflection": step["reflection"],
                "created_at": step["created_at"],
                "people": step.get("people") or [],
                "people_label": step.get("people_label") or "",
            }
        )
        source = previous_by_thread.get(thread_id) or f"thread-{thread_id}"
        edges.append(
            {
                "id": f"edge-{source}-{node_id}",
                "source": source,
                "target": node_id,
                "label": step["cause"] or step["step_type"],
                "step_type": step["step_type"],
            }
        )
        previous_by_thread[thread_id] = node_id
    return {"threads": thread_rows, "steps": steps, "nodes": nodes, "edges": edges}
