"""Orchestration for model-assisted memory consolidation."""

from __future__ import annotations

from datetime import datetime

from .audit import record_memory_mutation
from .event_compaction import run_event_thread_compaction
from .fact_compaction import run_fact_compaction
from .model_call import call_maintenance_model_json
from .prompt import SUMMARY_MAINTENANCE_PROMPT
from .snapshot import _normalize_int_list, _should_run_model_compaction


def _run_model_compaction(
    conn,
    snapshot: dict,
    *,
    apply: bool = True,
    run_date: str | None = None,
    trigger: str = "manual",
) -> dict:
    run_date = run_date or datetime.now().date().isoformat()
    if not _should_run_model_compaction(snapshot):
        return _empty_result()

    session_id = snapshot["session_id"]
    print(f"[pupu][maintenance] session={session_id} phase=summary start")
    summary_result = _run_summary_compaction(
        conn,
        snapshot,
        apply=apply,
        run_date=run_date,
        trigger=trigger,
    )
    print(
        f"[pupu][maintenance] session={session_id} phase=summary done "
        f"dropped={summary_result['dropped_summaries']} "
        f"merged={summary_result['merged_summaries']}"
    )
    if apply:
        conn.commit()

    print(f"[pupu][maintenance] session={session_id} phase=event_threads start")
    event_result = run_event_thread_compaction(
        conn,
        snapshot,
        apply=apply,
        run_date=run_date,
        trigger=trigger,
    )
    print(
        f"[pupu][maintenance] session={session_id} phase=event_threads done "
        f"updated={event_result['updated_event_threads']} "
        f"merged={event_result['merged_event_threads']} "
        f"rollups={event_result['event_rollups']}"
    )
    if apply:
        conn.commit()

    print(f"[pupu][maintenance] session={session_id} phase=facts start")
    fact_result = run_fact_compaction(
        conn,
        snapshot,
        apply=apply,
        run_date=run_date,
        trigger=trigger,
    )
    print(
        f"[pupu][maintenance] session={session_id} phase=facts done "
        f"archived={fact_result['deleted_facts']} "
        f"updated={fact_result['updated_facts']}"
    )
    if apply:
        conn.commit()

    notes = [
        note
        for note in (
            summary_result["note"],
            event_result["note"],
            fact_result["note"],
        )
        if note
    ]
    return {
        "dropped_summaries": summary_result["dropped_summaries"],
        "merged_summaries": summary_result["merged_summaries"],
        "updated_event_threads": event_result["updated_event_threads"],
        "merged_event_threads": event_result["merged_event_threads"],
        "event_rollups": event_result["event_rollups"],
        "deleted_facts": fact_result["deleted_facts"],
        "updated_facts": fact_result["updated_facts"],
        "fact_conflicts": fact_result["fact_conflicts"],
        "note": " | ".join(notes),
    }


def _empty_result() -> dict:
    return {
        "dropped_summaries": 0,
        "merged_summaries": 0,
        "updated_event_threads": 0,
        "merged_event_threads": 0,
        "event_rollups": 0,
        "deleted_facts": 0,
        "updated_facts": 0,
        "fact_conflicts": 0,
        "note": "",
    }


def _call_model_json(
    system_prompt: str,
    payload: dict,
    max_tokens: int = 5000,
    task_name: str = "maintenance",
) -> dict:
    """Compatibility wrapper retained for focused tests and integrations."""

    return call_maintenance_model_json(
        system_prompt,
        payload,
        max_tokens=max_tokens,
        task_name=task_name,
    )


def _run_summary_compaction(
    conn,
    snapshot: dict,
    *,
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict:
    summaries = snapshot["summaries"]
    if len(summaries) < 2:
        return {"dropped_summaries": 0, "merged_summaries": 0, "note": ""}

    result = call_maintenance_model_json(
        SUMMARY_MAINTENANCE_PROMPT,
        {
            "session_id": snapshot["session_id"],
            "summaries": summaries,
            "owner_facts": snapshot["owner_facts"],
            "instance_facts": snapshot["instance_facts"],
        },
        max_tokens=900,
        task_name="summary_maintenance",
    )
    summary_rows = {row["id"]: row for row in summaries}
    drop_ids = _normalize_int_list(
        result.get("drop_summary_ids", []),
        set(summary_rows),
    )
    merged_summary = str(result.get("merged_summary", "")).strip()
    note = str(result.get("notes", "")).strip()
    merged_saved = int(bool(merged_summary and len(drop_ids) >= 2))
    if not merged_saved:
        drop_ids = []

    if apply and merged_saved:
        source_rows = [summary_rows[summary_id] for summary_id in drop_ids]
        conn.execute(
            """INSERT INTO summaries
               (session_id, summary, start_msg_id, end_msg_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot["session_id"],
                merged_summary,
                min(row["start_msg_id"] for row in source_rows),
                max(row["end_msg_id"] for row in source_rows),
                datetime.now().isoformat(),
            ),
        )
        placeholders = ",".join("?" for _ in drop_ids)
        deleted = conn.execute(
            f"DELETE FROM summaries WHERE id IN ({placeholders})",
            drop_ids,
        ).rowcount
        record_memory_mutation(
            conn,
            run_date=run_date,
            trigger=trigger,
            session_id=snapshot["session_id"],
            entity_type="summary",
            entity_id=",".join(str(value) for value in drop_ids),
            action="merge",
            before=source_rows,
            after={"summary": merged_summary},
            evidence=drop_ids,
            reason=note,
            confidence=1.0,
        )
    else:
        deleted = len(drop_ids)

    return {
        "dropped_summaries": deleted,
        "merged_summaries": merged_saved,
        "note": note,
    }
