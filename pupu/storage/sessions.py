"""Session-wide maintenance helpers."""

from __future__ import annotations

from .db import get_conn
from .event_threads import EVENT_THREAD_FTS_TABLE, _event_thread_fts_available
from .people import INSTANCE_PERSON_KEY, OWNER_PERSON_KEY, person_from_session


def reset_session(session_id: str):
    conn = get_conn()
    thread_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM event_threads WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    ]
    if _event_thread_fts_available(conn):
        conn.execute(f"DELETE FROM {EVENT_THREAD_FTS_TABLE} WHERE session_id = ?", (session_id,))
    if thread_ids:
        placeholders = ",".join("?" for _ in thread_ids)
        conn.execute(
            f"DELETE FROM event_people WHERE thread_id IN ({placeholders})",
            tuple(thread_ids),
        )
        conn.execute(
            f"DELETE FROM event_thread_rollups WHERE thread_id IN ({placeholders})",
            tuple(thread_ids),
        )
    conn.execute(
        "DELETE FROM event_thread_aliases WHERE session_id = ?",
        (session_id,),
    )
    conn.execute(
        """DELETE FROM event_steps
           WHERE thread_id IN (
               SELECT id FROM event_threads WHERE session_id = ?
           )""",
        (session_id,),
    )
    for table in (
        "messages",
        "archived_messages",
        "familiarity",
        "events",
        "event_threads",
        "summaries",
        "scheduled_tasks",
    ):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    conn.execute(
        "DELETE FROM semantic_sync_log WHERE context_session = ? OR identity_session = ?",
        (session_id, session_id),
    )
    subject_key = person_from_session(session_id)
    fact_ids = [
        int(row["id"])
        for row in conn.execute(
            """SELECT id FROM person_facts
               WHERE subject_person_key = ? OR object_person_key = ?
                  OR source_context_session = ?""",
            (subject_key, subject_key, session_id),
        ).fetchall()
    ]
    if session_id == "owner":
        fact_ids.extend(
            int(row["id"])
            for row in conn.execute(
                """SELECT id FROM person_facts
                   WHERE subject_person_key IN (?, ?)
                      OR object_person_key IN (?, ?)""",
                (
                    OWNER_PERSON_KEY,
                    INSTANCE_PERSON_KEY,
                    OWNER_PERSON_KEY,
                    INSTANCE_PERSON_KEY,
                ),
            ).fetchall()
        )
    if fact_ids:
        placeholders = ",".join("?" for _ in set(fact_ids))
        conn.execute(
            f"DELETE FROM fact_evidence WHERE fact_id IN ({placeholders})",
            tuple(sorted(set(fact_ids))),
        )
    conn.execute(
        """DELETE FROM person_facts
           WHERE subject_person_key = ?
              OR object_person_key = ?
              OR source_context_session = ?""",
        (
            subject_key,
            subject_key,
            session_id,
        ),
    )
    if session_id == "owner":
        conn.execute(
            """DELETE FROM person_facts
               WHERE subject_person_key IN (?, ?)
                  OR object_person_key IN (?, ?)""",
            (OWNER_PERSON_KEY, INSTANCE_PERSON_KEY, OWNER_PERSON_KEY, INSTANCE_PERSON_KEY),
        )
    conn.execute(
        "DELETE FROM memory_mutations WHERE session_id = ?",
        (session_id,),
    )
    conn.commit()
    conn.close()
