"""Maintenance run bookkeeping helpers."""

from datetime import datetime

from ..sessions import OWNER_SESSION
from ..storage.people import INSTANCE_PERSON_KEY, OWNER_PERSON_KEY


def _record_maintenance_run(
    conn,
    run_date: str,
    trigger: str,
    status: str,
    report: str,
) -> None:
    conn.execute(
        """INSERT INTO maintenance_runs
           (run_date, trigger, status, report, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (run_date, trigger, status, report, datetime.now().isoformat()),
    )


def _has_successful_auto_run(conn, run_date: str) -> bool:
    row = conn.execute(
        """SELECT 1
           FROM maintenance_runs
           WHERE run_date = ? AND trigger = 'auto' AND status = 'success'
           ORDER BY id DESC
           LIMIT 1""",
        (run_date,),
    ).fetchone()
    return bool(row)


def _list_all_session_ids(conn) -> list[str]:
    session_ids = set()
    for table in (
        "messages",
        "familiarity",
        "events",
        "summaries",
        "scheduled_tasks",
        "event_threads",
    ):
        rows = conn.execute(f"SELECT DISTINCT session_id FROM {table}").fetchall()
        session_ids.update(row["session_id"] for row in rows if row["session_id"])
    rows = conn.execute(
        """SELECT DISTINCT source_context_session AS session_id
           FROM person_facts
           WHERE source_context_session != ''"""
    ).fetchall()
    session_ids.update(row["session_id"] for row in rows if row["session_id"])
    shared_fact = conn.execute(
        """SELECT 1 FROM person_facts
           WHERE status = 'active' AND subject_person_key IN (?, ?)
           LIMIT 1""",
        (OWNER_PERSON_KEY, INSTANCE_PERSON_KEY),
    ).fetchone()
    if shared_fact:
        session_ids.add(OWNER_SESSION)
    return sorted(session_ids)
