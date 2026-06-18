"""Snapshot builders for model-assisted maintenance."""

from ..storage.event_threads import get_recent_event_threads_from_conn
from ..storage.people import INSTANCE_PERSON_KEY, person_from_session


def _build_session_snapshot(conn, session_id: str) -> dict:
    summaries = [
        dict(row)
        for row in conn.execute(
            """SELECT id, summary, start_msg_id, end_msg_id, created_at
               FROM summaries
               WHERE session_id = ?
               ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
    ]
    subject_key = person_from_session(session_id)
    owner_facts = _person_fact_map(conn, subject_key)
    instance_facts = _person_fact_map(conn, INSTANCE_PERSON_KEY)
    tasks = [
        dict(row)
        for row in conn.execute(
            """SELECT id, title, instruction, run_at, repeat_kind, interval_seconds
               FROM scheduled_tasks
               WHERE session_id = ? AND enabled = 1
               ORDER BY run_at ASC, id ASC""",
            (session_id,),
        ).fetchall()
    ]
    event_threads = list(
        reversed(get_recent_event_threads_from_conn(conn, session_id, limit=None))
    )
    return {
        "session_id": session_id,
        "summaries": summaries,
        "owner_facts": owner_facts,
        "instance_facts": instance_facts,
        "tasks": tasks,
        "event_threads": event_threads,
    }


def _person_fact_map(conn, subject_person_key: str) -> dict[str, str]:
    return {
        row["fact_key"]: row["fact_value"]
        for row in conn.execute(
            """SELECT fact_key, fact_value
               FROM person_facts
               WHERE subject_person_key = ?
                 AND object_person_key = ''
                 AND scope = 'person'
               ORDER BY updated_at ASC""",
            (subject_person_key,),
        ).fetchall()
    }


def _normalize_int_list(values, allowed_ids: set[int]) -> list[int]:
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        try:
            parsed = int(value)
        except Exception:
            continue
        if parsed in allowed_ids and parsed not in out:
            out.append(parsed)
    return out


def _should_run_model_compaction(snapshot: dict) -> bool:
    return bool(
        snapshot["summaries"]
        or snapshot["event_threads"]
        or snapshot["tasks"]
        or snapshot["owner_facts"]
        or snapshot["instance_facts"]
    )
