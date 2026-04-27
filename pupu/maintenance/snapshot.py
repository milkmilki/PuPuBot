"""Snapshot builders for model-assisted maintenance."""


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
    user_facts = {
        row["fact_key"]: row["fact_value"]
        for row in conn.execute(
            """SELECT fact_key, fact_value
               FROM user_facts
               WHERE session_id = ?
               ORDER BY updated_at ASC""",
            (session_id,),
        ).fetchall()
    }
    self_facts = {
        row["fact_key"]: row["fact_value"]
        for row in conn.execute(
            """SELECT fact_key, fact_value
               FROM self_facts
               WHERE session_id = ?
               ORDER BY updated_at ASC""",
            (session_id,),
        ).fetchall()
    }
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
    important_events = [
        dict(row)
        for row in conn.execute(
            """SELECT id, source_event_key, title, kind, event_time, time_text,
                      details, followup_hint, confidence, status, linked_task_id,
                      last_seen_at, created_at
               FROM important_events
               WHERE session_id = ?
               ORDER BY created_at ASC, id ASC""",
            (session_id,),
        ).fetchall()
    ]
    return {
        "session_id": session_id,
        "summaries": summaries,
        "user_facts": user_facts,
        "self_facts": self_facts,
        "tasks": tasks,
        "important_events": important_events,
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
        or snapshot["important_events"]
        or snapshot["tasks"]
        or snapshot["user_facts"]
        or snapshot["self_facts"]
    )
