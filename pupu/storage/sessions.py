"""Session-wide maintenance helpers."""

from __future__ import annotations

from .db import get_conn


def reset_session(session_id: str):
    conn = get_conn()
    for table in (
        "messages",
        "familiarity",
        "events",
        "important_events",
        "user_facts",
        "summaries",
        "self_facts",
        "scheduled_tasks",
    ):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
