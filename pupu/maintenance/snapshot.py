"""Snapshot builders for model-assisted maintenance."""

from ..sessions import OWNER_SESSION
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
    facts = _facts_for_session(conn, session_id, subject_key)
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
    for event in event_threads:
        compacted_through = int(event.get("compacted_through_step_id") or 0)
        event["maintenance_steps"] = [
            dict(row)
            for row in conn.execute(
                """SELECT id, step_type, summary, cause, reflection, occurred_at,
                          source_msg_start_id, source_msg_end_id, created_at
                   FROM event_steps
                   WHERE thread_id = ? AND id > ?
                   ORDER BY id ASC""",
                (int(event["id"]), compacted_through),
            ).fetchall()
        ]
    return {
        "session_id": session_id,
        "summaries": summaries,
        "owner_facts": owner_facts,
        "instance_facts": instance_facts,
        "facts": facts,
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
                 AND status = 'active'
                 AND object_person_key = ''
                 AND scope = 'person'
               ORDER BY updated_at ASC""",
            (subject_person_key,),
        ).fetchall()
    }


def _facts_for_session(
    conn,
    session_id: str,
    subject_person_key: str,
) -> list[dict]:
    include_instance = session_id == OWNER_SESSION
    rows = conn.execute(
        """SELECT pf.*,
                  sp.display_name AS subject_display_name,
                  op.display_name AS object_display_name
           FROM person_facts pf
           LEFT JOIN people sp ON sp.person_key = pf.subject_person_key
           LEFT JOIN people op ON op.person_key = pf.object_person_key
           WHERE pf.status = 'active'
             AND (
                 pf.source_context_session = ?
                 OR pf.subject_person_key = ?
                 OR pf.object_person_key = ?
                 OR ? = 1
             )
           ORDER BY pf.subject_person_key, pf.object_person_key,
                    pf.scope, pf.updated_at, pf.id""",
        (
            session_id,
            subject_person_key,
            subject_person_key,
            1 if include_instance else 0,
        ),
    ).fetchall()
    facts = [dict(row) for row in rows]
    if not facts:
        return []
    fact_ids = [int(row["id"]) for row in facts]
    placeholders = ",".join("?" for _ in fact_ids)
    evidence_rows = conn.execute(
        f"""SELECT fact_id, source_context_session, source_msg_start_id,
                   source_msg_end_id, evidence_text, created_at
            FROM fact_evidence
            WHERE fact_id IN ({placeholders})
            ORDER BY fact_id, id""",
        tuple(fact_ids),
    ).fetchall()
    evidence_by_fact: dict[int, list[dict]] = {}
    for evidence in evidence_rows:
        item = dict(evidence)
        item["source_excerpt"] = _fact_evidence_excerpt(conn, item)
        evidence_by_fact.setdefault(int(evidence["fact_id"]), []).append(item)
    for fact in facts:
        fact["evidence"] = evidence_by_fact.get(int(fact["id"]), [])[-8:]
    return [
        fact
        for fact in facts
        if not (
            fact["evidence"]
            and all(
                evidence.get("source_context_session") in {"persona_seed", "legacy"}
                for evidence in fact["evidence"]
            )
            and any(
                evidence.get("source_context_session") == "persona_seed"
                for evidence in fact["evidence"]
            )
        )
    ]


def _fact_evidence_excerpt(conn, evidence: dict) -> str:
    session_id = str(evidence.get("source_context_session") or "").strip()
    start_id = evidence.get("source_msg_start_id")
    end_id = evidence.get("source_msg_end_id")
    if not session_id or start_id is None or end_id is None:
        return ""
    rows = conn.execute(
        """SELECT id, role, content
           FROM (
               SELECT id, role, content FROM messages
               WHERE session_id = ? AND id BETWEEN ? AND ?
               UNION ALL
               SELECT id, role, content FROM archived_messages
               WHERE session_id = ? AND id BETWEEN ? AND ?
           )
           ORDER BY id ASC
           LIMIT 8""",
        (
            session_id,
            int(start_id),
            int(end_id),
            session_id,
            int(start_id),
            int(end_id),
        ),
    ).fetchall()
    text = " | ".join(
        f"{row['role']}: {str(row['content'] or '').strip()}"
        for row in rows
        if str(row["content"] or "").strip()
    )
    return text[:600]


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
        or snapshot["facts"]
    )
