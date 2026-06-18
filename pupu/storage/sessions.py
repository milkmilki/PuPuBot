"""Session-wide maintenance helpers."""

from __future__ import annotations

from .db import get_conn
from .event_threads import EVENT_THREAD_FTS_TABLE, _event_thread_fts_available
from .people import INSTANCE_PERSON_KEY, OWNER_PERSON_KEY, person_from_session


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
        "summaries",
        "scheduled_tasks",
    ):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    conn.execute(
        "DELETE FROM memu_sync_log WHERE context_session = ? OR identity_session = ?",
        (session_id, session_id),
    )
    subject_key = person_from_session(session_id)
    conn.execute(
        """DELETE FROM person_facts
           WHERE legacy_session_id = ?
              OR subject_person_key = ?
              OR object_person_key = ?""",
        (
            session_id,
            subject_key,
            subject_key,
        ),
    )
    if session_id == "owner":
        conn.execute(
            """DELETE FROM person_facts
               WHERE subject_person_key IN (?, ?)
                  OR object_person_key IN (?, ?)""",
            (OWNER_PERSON_KEY, INSTANCE_PERSON_KEY, OWNER_PERSON_KEY, INSTANCE_PERSON_KEY),
        )
    conn.commit()
    conn.close()
