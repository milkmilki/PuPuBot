"""Session-wide maintenance helpers."""

from __future__ import annotations

from .db import get_conn
from .event_threads import EVENT_THREAD_FTS_TABLE, _event_thread_fts_available


def reset_session(session_id: str):
    conn = get_conn()
    if _event_thread_fts_available(conn):
        conn.execute(f"DELETE FROM {EVENT_THREAD_FTS_TABLE} WHERE session_id = ?", (session_id,))
    conn.execute(
        """DELETE FROM event_steps
           WHERE thread_id IN (
               SELECT id FROM event_threads WHERE session_id = ?
           )""",
        (session_id,),
    )
    for table in (
        "messages",
        "familiarity",
        "events",
        "event_threads",
        "user_facts",
        "summaries",
        "self_facts",
        "scheduled_tasks",
    ):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    conn.execute(
        "DELETE FROM memu_sync_log WHERE context_session = ? OR identity_session = ?",
        (session_id, session_id),
    )
    conn.commit()
    conn.close()
