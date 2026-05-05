"""Persistence helpers for raw conversation messages."""

from __future__ import annotations

from datetime import datetime

from ..message_sources import CHAT
from .db import get_conn
from .summaries import get_oldest_unsummarized_msg_id


def _resolve_context_session(session_id: str = "default", context_session: str | None = None) -> str:
    return str(context_session or session_id or "default")


def save_message(
    role: str,
    content: str,
    session_id: str = "default",
    source: str = CHAT,
    *,
    context_session: str | None = None,
):
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, source) VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, datetime.now().isoformat(), source),
    )
    conn.commit()
    conn.close()


def get_recent_messages(
    n: int = 50,
    session_id: str = "default",
    *,
    context_session: str | None = None,
) -> list[dict]:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, n),
    ).fetchall()
    conn.close()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def get_messages_in_range(
    session_id: str,
    after_id: int,
    limit: int = 100,
    *,
    context_session: str | None = None,
) -> list[dict]:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (session_id, after_id, limit),
    ).fetchall()
    conn.close()
    return [{"id": row["id"], "role": row["role"], "content": row["content"]} for row in rows]


def count_pending_review_turns(
    session_id: str = "default",
    after_msg_id: int = 0,
    source: str = CHAT,
    *,
    context_session: str | None = None,
) -> int:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) AS cnt
           FROM messages
           WHERE session_id = ?
             AND source = ?
             AND role = 'assistant'
             AND id > ?""",
        (session_id, source, after_msg_id),
    ).fetchone()
    conn.close()
    return int(row["cnt"]) if row else 0


def get_review_candidate_batch(
    session_id: str = "default",
    review_interval: int = 8,
    source: str = CHAT,
    min_turns: int | None = None,
    *,
    context_session: str | None = None,
) -> list[dict]:
    session_id = _resolve_context_session(session_id, context_session)
    interval = max(1, int(review_interval or 1))
    minimum = interval if min_turns is None else max(1, int(min_turns or 1))
    after_msg_id = get_oldest_unsummarized_msg_id(session_id)

    conn = get_conn()
    assistant_rows = conn.execute(
        """SELECT id
           FROM messages
           WHERE session_id = ?
             AND source = ?
             AND role = 'assistant'
             AND id > ?
           ORDER BY id ASC
           LIMIT ?""",
        (session_id, source, after_msg_id, interval),
    ).fetchall()

    if len(assistant_rows) < minimum:
        conn.close()
        return []

    end_msg_id = assistant_rows[-1]["id"]
    rows = conn.execute(
        """SELECT id, role, content, source
           FROM messages
           WHERE session_id = ?
             AND source = ?
             AND id > ?
             AND id <= ?
           ORDER BY id ASC""",
        (session_id, source, after_msg_id, end_msg_id),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_pending_review_last_message_time(
    session_id: str = "default",
    after_msg_id: int = 0,
    source: str = CHAT,
    *,
    context_session: str | None = None,
) -> str | None:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    row = conn.execute(
        """SELECT timestamp
           FROM messages
           WHERE session_id = ?
             AND source = ?
             AND id > ?
           ORDER BY id DESC
           LIMIT 1""",
        (session_id, source, after_msg_id),
    ).fetchone()
    conn.close()
    return row["timestamp"] if row else None


def list_pending_review_sessions(source: str = CHAT) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT m.session_id
           FROM messages m
           LEFT JOIN (
             SELECT session_id, MAX(end_msg_id) AS last_reviewed_id
             FROM summaries
             GROUP BY session_id
           ) s ON s.session_id = m.session_id
           WHERE m.source = ?
             AND m.role = 'assistant'
             AND m.id > COALESCE(s.last_reviewed_id, 0)
           GROUP BY m.session_id
           ORDER BY m.session_id ASC""",
        (source,),
    ).fetchall()
    conn.close()
    return [str(row["session_id"]) for row in rows]


def get_summary_trigger_progress(
    session_id: str = "default",
    review_interval: int = 8,
    *,
    context_session: str | None = None,
) -> dict[str, int | bool]:
    session_id = _resolve_context_session(session_id, context_session)
    interval = max(1, int(review_interval or 1))
    last_reviewed_id = get_oldest_unsummarized_msg_id(session_id)
    pending = count_pending_review_turns(
        session_id=session_id,
        after_msg_id=last_reviewed_id,
        source=CHAT,
    )
    remaining = max(0, interval - pending)
    return {
        "pending": pending,
        "remaining": remaining,
        "interval": interval,
        "ready": remaining == 0,
    }


def count_messages(session_id: str = "default", *, context_session: str | None = None) -> int:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_last_user_message_time(
    session_id: str = "default",
    *,
    context_session: str | None = None,
) -> str | None:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    row = conn.execute(
        "SELECT timestamp FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["timestamp"] if row else None


def get_last_message_time(
    session_id: str = "default",
    *,
    context_session: str | None = None,
) -> str | None:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    row = conn.execute(
        "SELECT timestamp FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["timestamp"] if row else None
