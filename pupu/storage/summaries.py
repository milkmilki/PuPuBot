"""Persistence helpers for conversation summaries."""

from __future__ import annotations

from datetime import datetime

from .db import get_conn


def _resolve_context_session(session_id: str = "default", context_session: str | None = None) -> str:
    return str(context_session or session_id or "default")


def get_oldest_unsummarized_msg_id(
    session_id: str = "default",
    *,
    context_session: str | None = None,
) -> int:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(end_msg_id) AS last_end FROM summaries WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    last_end = row["last_end"] if row and row["last_end"] else 0
    conn.close()
    return last_end


def save_summary(
    summary: str,
    start_msg_id: int,
    end_msg_id: int,
    session_id: str = "default",
    *,
    context_session: str | None = None,
):
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    conn.execute(
        "INSERT INTO summaries (session_id, summary, start_msg_id, end_msg_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, summary, start_msg_id, end_msg_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_summaries(
    session_id: str = "default",
    limit: int = 5,
    *,
    context_session: str | None = None,
) -> list[dict]:
    session_id = _resolve_context_session(session_id, context_session)
    conn = get_conn()
    rows = conn.execute(
        "SELECT summary, created_at FROM summaries WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"summary": row["summary"], "created_at": row["created_at"]} for row in reversed(rows)]
