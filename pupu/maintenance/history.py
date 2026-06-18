"""Maintenance run bookkeeping helpers."""

from datetime import datetime


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
        "user_facts",
        "self_facts",
        "summaries",
        "scheduled_tasks",
        "event_threads",
    ):
        rows = conn.execute(f"SELECT DISTINCT session_id FROM {table}").fetchall()
        session_ids.update(row["session_id"] for row in rows if row["session_id"])
    return sorted(session_ids)
