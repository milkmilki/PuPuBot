"""Model-assisted memory compaction helpers."""

from __future__ import annotations

import json
from datetime import datetime

from ..llm import MODEL, json_task
from ..storage.event_threads import apply_event_thread_maintenance
from ..storage.people import INSTANCE_PERSON_KEY, person_from_session
from .parsing import _parse_json_object
from .prompt import (
    FACTS_MAINTENANCE_PROMPT,
    EVENT_THREAD_MAINTENANCE_PROMPT,
    SUMMARY_MAINTENANCE_PROMPT,
)
from .snapshot import _normalize_int_list, _should_run_model_compaction

EVENT_THREAD_CHUNK_SIZE = 12


def _commit_quietly(conn) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def _run_model_compaction(conn, snapshot: dict, *, apply: bool = True) -> dict:
    if not _should_run_model_compaction(snapshot):
        return {
            "dropped_summaries": 0,
            "merged_summaries": 0,
            "updated_event_threads": 0,
            "deleted_facts": 0,
            "updated_facts": 0,
            "note": "",
        }

    session_id = snapshot["session_id"]
    print(f"[pupu][maintenance] session={session_id} phase=summary start")
    summary_result = _run_summary_compaction(conn, snapshot, apply=apply)
    print(
        f"[pupu][maintenance] session={session_id} phase=summary done "
        f"dropped={summary_result['dropped_summaries']} merged={summary_result['merged_summaries']}"
    )
    if apply:
        _commit_quietly(conn)
    print(f"[pupu][maintenance] session={session_id} phase=event_threads start")
    event_thread_result = _run_event_thread_compaction(conn, snapshot, apply=apply)
    print(
        f"[pupu][maintenance] session={session_id} phase=event_threads done "
        f"updated={event_thread_result['updated_event_threads']}"
    )
    if apply:
        _commit_quietly(conn)
    print(f"[pupu][maintenance] session={session_id} phase=facts start")
    fact_result = _run_fact_compaction(conn, snapshot, apply=apply)
    print(
        f"[pupu][maintenance] session={session_id} phase=facts done "
        f"deleted={fact_result['deleted_facts']} updated={fact_result['updated_facts']}"
    )
    if apply:
        _commit_quietly(conn)

    notes = [
        part
        for part in (
            summary_result["note"],
            event_thread_result["note"],
            fact_result["note"],
        )
        if part
    ]
    return {
        "dropped_summaries": summary_result["dropped_summaries"],
        "merged_summaries": summary_result["merged_summaries"],
        "updated_event_threads": event_thread_result["updated_event_threads"],
        "deleted_facts": fact_result["deleted_facts"],
        "updated_facts": fact_result["updated_facts"],
        "note": " | ".join(notes),
    }


def _call_model_json(
    system_prompt: str,
    payload: dict,
    max_tokens: int = 5000,
    task_name: str = "maintenance",
) -> dict:
    raw_text = json_task(
        role="maintenance",
        model=MODEL,
        system=system_prompt,
        user_content=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=max_tokens,
        task_name=task_name,
    )
    try:
        return _parse_json_object(raw_text)
    except Exception as exc:
        preview = (raw_text or "").strip().replace("\n", " ")[:300]
        raise ValueError(
            f"{task_name}: unable to parse maintenance response as JSON object; preview={preview!r}"
        ) from exc


def _run_summary_compaction(conn, snapshot: dict, *, apply: bool = True) -> dict:
    summaries = snapshot["summaries"]
    if len(summaries) < 2:
        return {"dropped_summaries": 0, "merged_summaries": 0, "note": ""}

    payload = {
        "session_id": snapshot["session_id"],
        "summaries": summaries,
        "owner_facts": snapshot["owner_facts"],
        "instance_facts": snapshot["instance_facts"],
    }
    result = _call_model_json(
        SUMMARY_MAINTENANCE_PROMPT,
        payload,
        max_tokens=900,
        task_name="summary_maintenance",
    )

    summary_rows = {row["id"]: row for row in summaries}
    drop_summary_ids = _normalize_int_list(
        result.get("drop_summary_ids", []),
        set(summary_rows.keys()),
    )
    merged_summary = str(result.get("merged_summary", "")).strip()
    note = str(result.get("notes", "")).strip()

    merged_saved = 1 if merged_summary and len(drop_summary_ids) >= 2 else 0
    if merged_saved and apply:
        source_rows = [summary_rows[summary_id] for summary_id in drop_summary_ids]
        start_msg_id = min(row["start_msg_id"] for row in source_rows)
        end_msg_id = max(row["end_msg_id"] for row in source_rows)
        conn.execute(
            """INSERT INTO summaries
               (session_id, summary, start_msg_id, end_msg_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot["session_id"],
                merged_summary,
                start_msg_id,
                end_msg_id,
                datetime.now().isoformat(),
            ),
        )

    dropped_summaries = len(drop_summary_ids)
    if apply and drop_summary_ids:
        placeholders = ",".join("?" for _ in drop_summary_ids)
        cur = conn.execute(
            f"DELETE FROM summaries WHERE id IN ({placeholders})",
            drop_summary_ids,
        )
        dropped_summaries = cur.rowcount

    return {
        "dropped_summaries": dropped_summaries,
        "merged_summaries": merged_saved,
        "note": note,
    }


def _run_event_thread_compaction(conn, snapshot: dict, *, apply: bool = True) -> dict:
    events = snapshot["event_threads"]
    if not events:
        return {
            "updated_event_threads": 0,
            "note": "",
        }

    updated_total = 0
    notes: list[str] = []
    tasks = snapshot["tasks"]

    for index in range(0, len(events), EVENT_THREAD_CHUNK_SIZE):
        chunk = events[index : index + EVENT_THREAD_CHUNK_SIZE]
        chunk_by_id = {int(row["id"]): row for row in chunk}
        payload = {
            "session_id": snapshot["session_id"],
            "now": datetime.now().isoformat(timespec="seconds"),
            "event_threads": chunk,
            "tasks": tasks,
        }
        result = _call_model_json(
            EVENT_THREAD_MAINTENANCE_PROMPT,
            payload,
            max_tokens=5000,
            task_name="event_thread_maintenance",
        )

        updates = _normalize_event_thread_updates(
            result.get("updates", []),
            set(chunk_by_id.keys()),
        )
        note = str(result.get("notes", "")).strip()
        if note:
            notes.append(note)

        if apply:
            updated = apply_event_thread_maintenance(
                conn,
                snapshot["session_id"],
                updates=updates,
                now=datetime.now().isoformat(),
            )
            updated_total += updated
        else:
            updated_total += len(updates)

        if apply and updates:
            _commit_quietly(conn)

    return {
        "updated_event_threads": updated_total,
        "note": " | ".join(notes[:4]),
    }


def _run_fact_compaction(conn, snapshot: dict, *, apply: bool = True) -> dict:
    owner_facts = snapshot["owner_facts"]
    instance_facts = snapshot["instance_facts"]
    if not owner_facts and not instance_facts:
        return {"deleted_facts": 0, "updated_facts": 0, "note": ""}

    session_id = snapshot["session_id"]
    payload = {
        "session_id": session_id,
        "owner_facts": owner_facts,
        "instance_facts": instance_facts,
    }
    result = _call_model_json(
        FACTS_MAINTENANCE_PROMPT,
        payload,
        max_tokens=5000,
        task_name="facts_maintenance",
    )

    owner_result = _apply_fact_updates(
        conn,
        session_id=session_id,
        subject_person_key=person_from_session(session_id),
        existing_facts=owner_facts,
        updates=result.get("owner_updates", {}),
        delete_keys=result.get("owner_delete_keys", []),
        apply=apply,
    )
    instance_result = _apply_fact_updates(
        conn,
        session_id=session_id,
        subject_person_key=INSTANCE_PERSON_KEY,
        existing_facts=instance_facts,
        updates=result.get("instance_updates", {}),
        delete_keys=result.get("instance_delete_keys", []),
        apply=apply,
    )

    return {
        "deleted_facts": owner_result["deleted"] + instance_result["deleted"],
        "updated_facts": owner_result["updated"] + instance_result["updated"],
        "note": str(result.get("notes", "")).strip(),
    }


def _apply_fact_updates(
    conn,
    session_id: str,
    subject_person_key: str,
    existing_facts: dict,
    updates,
    delete_keys,
    *,
    apply: bool = True,
) -> dict:
    allowed_keys = {str(key) for key in existing_facts.keys()}
    normalized_updates = _normalize_fact_updates(updates, allowed_keys)
    updated_keys = set(normalized_updates.keys())
    normalized_delete_keys = [
        key
        for key in _normalize_fact_delete_keys(delete_keys, allowed_keys)
        if key not in updated_keys
    ]

    now = datetime.now().isoformat()
    updated = 0
    for key, value in normalized_updates.items():
        if value == str(existing_facts.get(key, "")).strip():
            continue
        if apply:
            updated += _sync_person_fact(conn, session_id, subject_person_key, key, value)
        else:
            updated += 1

    deleted = len(normalized_delete_keys)
    if apply and normalized_delete_keys:
        deleted = 0
        for key in normalized_delete_keys:
            deleted += _sync_person_fact(conn, session_id, subject_person_key, key, "")

    return {"deleted": deleted, "updated": updated}


def _sync_person_fact(
    conn,
    session_id: str,
    subject_person_key: str,
    key: str,
    value: str,
) -> int:
    if not value:
        cur = conn.execute(
            """DELETE FROM person_facts
               WHERE subject_person_key = ? AND object_person_key = ''
                 AND scope = 'person' AND fact_key = ?""",
            (subject_person_key, key),
        )
        return max(0, int(cur.rowcount))
    now = datetime.now().isoformat()
    before = conn.total_changes
    conn.execute(
        """INSERT INTO person_facts (
               subject_person_key, object_person_key, scope,
               fact_key, fact_value, confidence, source_context_session,
               source_msg_start_id, source_msg_end_id, created_at, updated_at
           ) VALUES (?, '', 'person', ?, ?, 1.0, '', NULL, NULL, ?, ?)
           ON CONFLICT(subject_person_key, object_person_key, scope, fact_key)
           DO UPDATE SET
               fact_value = excluded.fact_value,
               updated_at = excluded.updated_at""",
        (subject_person_key, key, value, now, now),
    )
    return max(0, conn.total_changes - before)


def _normalize_fact_updates(values, allowed_keys: set[str]) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}

    out = {}
    for key, value in values.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text in allowed_keys and value_text:
            out[key_text] = value_text
    return out


def _normalize_fact_delete_keys(values, allowed_keys: set[str]) -> list[str]:
    if not isinstance(values, list):
        return []

    out = []
    for value in values:
        key_text = str(value).strip()
        if key_text in allowed_keys and key_text not in out:
            out.append(key_text)
    return out


def _normalize_event_thread_updates(values, allowed_ids: set[int]) -> list[dict]:
    if not isinstance(values, list):
        return []

    out = []
    seen = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            event_id = int(item.get("id"))
        except Exception:
            continue
        if event_id not in allowed_ids or event_id in seen:
            continue
        seen.add(event_id)
        try:
            confidence = float(item.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        out.append(
            {
                "id": event_id,
                "title": str(item.get("title", "")).strip(),
                "kind": str(item.get("kind", "")).strip(),
                "event_time": str(item.get("event_time", "")).strip(),
                "time_text": str(item.get("time_text", "")).strip(),
                "details": str(item.get("details", "")).strip(),
                "followup_hint": str(item.get("followup_hint", "")).strip(),
                "confidence": confidence,
            }
        )
    return out
