"""Event-thread consolidation, merging and reversible rollups."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..storage.event_threads import (
    apply_event_thread_maintenance,
    apply_event_thread_rollup,
    merge_event_threads,
)
from ..storage.people import INSTANCE_PERSON_KEY
from .audit import record_memory_mutation
from .model_call import call_maintenance_model_json
from .prompt import (
    EVENT_THREAD_MAINTENANCE_PROMPT,
    EVENT_THREAD_MERGE_PROMPT,
    EVENT_THREAD_ROLLUP_PROMPT,
)
from .snapshot import _normalize_int_list

EVENT_THREAD_CHUNK_SIZE = 12
EVENT_ROLLUP_MIN_STEPS = 12
EVENT_ROLLUP_RECENT_STEPS = 4
EVENT_ROLLUP_BATCH_STEPS = 24


def run_event_thread_compaction(
    conn,
    snapshot: dict,
    *,
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict:
    events = snapshot["event_threads"]
    if not events:
        return _empty_result()

    updated_total = 0
    merged_total = 0
    rollup_total = 0
    merged_source_ids: set[int] = set()
    merged_canonical_ids: set[int] = set()
    notes: list[str] = []

    merge_candidates = _event_merge_candidates(events)
    merges: list[dict[str, Any]] = []
    if merge_candidates:
        merge_result = call_maintenance_model_json(
            EVENT_THREAD_MERGE_PROMPT,
            {
                "session_id": snapshot["session_id"],
                "now": datetime.now().isoformat(timespec="seconds"),
                "merge_candidates": merge_candidates,
            },
            max_tokens=4000,
            task_name="event_thread_merge_maintenance",
        )
        merges = _normalize_event_thread_merges(
            merge_result.get("merges", []),
            merge_candidates,
        )
        merge_note = str(merge_result.get("notes", "")).strip()
        if merge_note:
            notes.append(merge_note)

    rollup_plans: list[dict[str, Any]] = []
    for event in events:
        rollup = _plan_event_rollup(
            snapshot=snapshot,
            event=event,
        )
        if rollup:
            rollup_plans.append(rollup)

    update_plans: list[dict[str, Any]] = []
    for index in range(0, len(events), EVENT_THREAD_CHUNK_SIZE):
        chunk = events[index : index + EVENT_THREAD_CHUNK_SIZE]
        chunk_by_id = {int(row["id"]): row for row in chunk}
        result = call_maintenance_model_json(
            EVENT_THREAD_MAINTENANCE_PROMPT,
            {
                "session_id": snapshot["session_id"],
                "now": datetime.now().isoformat(timespec="seconds"),
                "event_threads": [_event_for_model(event) for event in chunk],
                "tasks": snapshot["tasks"],
            },
            max_tokens=5000,
            task_name="event_thread_maintenance",
        )
        updates = _normalize_event_thread_updates(
            result.get("updates", []),
            set(chunk_by_id),
        )
        note = str(result.get("notes", "")).strip()
        if note:
            notes.append(note)
        for update in updates:
            update_plans.append({**update, "_reason": note})

    for merge in merges:
        outcome = _apply_event_merge(
            conn,
            snapshot=snapshot,
            events=events,
            merge=merge,
            apply=apply,
            run_date=run_date,
            trigger=trigger,
        )
        merged_total += outcome["merged"]
        if outcome["merged"]:
            merged_source_ids.update(merge["source_ids"])
            merged_canonical_ids.add(merge["canonical_id"])

    merged_ids = merged_source_ids | merged_canonical_ids
    for rollup in rollup_plans:
        if int(rollup["thread_id"]) in merged_ids:
            continue
        rollup_total += _apply_event_rollup_plan(
            conn,
            snapshot=snapshot,
            plan=rollup,
            apply=apply,
            run_date=run_date,
            trigger=trigger,
        )

    for update in update_plans:
        if int(update["id"]) in merged_ids:
            continue
        if not apply:
            updated_total += 1
            continue
        before = _event_by_id(events, int(update["id"]))
        if not before or not _events_are_current(conn, [before]):
            continue
        updated = apply_event_thread_maintenance(
            conn,
            snapshot["session_id"],
            updates=[update],
            now=datetime.now().isoformat(),
        )
        if not updated:
            continue
        record_memory_mutation(
            conn,
            run_date=run_date,
            trigger=trigger,
            session_id=snapshot["session_id"],
            entity_type="event_thread",
            entity_id=update["id"],
            action="rewrite",
            before=before,
            after={
                key: value for key, value in update.items() if not key.startswith("_")
            },
            evidence=[update["id"]],
            reason=update["_reason"],
            confidence=update["confidence"],
        )
        updated_total += updated

    return {
        "updated_event_threads": updated_total,
        "merged_event_threads": merged_total,
        "event_rollups": rollup_total,
        "note": " | ".join(notes[:4]),
    }


def _empty_result() -> dict:
    return {
        "updated_event_threads": 0,
        "merged_event_threads": 0,
        "event_rollups": 0,
        "note": "",
    }


def _apply_event_merge(
    conn,
    *,
    snapshot: dict,
    events: list[dict[str, Any]],
    merge: dict[str, Any],
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict[str, int]:
    canonical_id = merge["canonical_id"]
    source_ids = merge["source_ids"]
    before = {
        "canonical": _event_by_id(events, canonical_id),
        "sources": [_event_by_id(events, source_id) for source_id in source_ids],
    }
    if apply and not _events_are_current(
        conn,
        [before["canonical"], *before["sources"]],
    ):
        return {"merged": 0}
    after = {
        "canonical_id": canonical_id,
        "source_ids": source_ids,
        "title": merge["title"],
        "details": merge["details"],
        "followup_hint": merge["followup_hint"],
    }
    if not apply:
        return {"merged": len(source_ids)}
    merged = merge_event_threads(
        conn,
        snapshot["session_id"],
        canonical_thread_id=canonical_id,
        source_thread_ids=source_ids,
        title=merge["title"],
        details=merge["details"],
        followup_hint=merge["followup_hint"],
        confidence=merge["confidence"],
        now=datetime.now().isoformat(),
    )
    if merged:
        record_memory_mutation(
            conn,
            run_date=run_date,
            trigger=trigger,
            session_id=snapshot["session_id"],
            entity_type="event_thread",
            entity_id=canonical_id,
            action="merge",
            before=before,
            after=after,
            evidence=[canonical_id, *source_ids],
            reason=merge["reason"],
            confidence=merge["confidence"],
        )
    return {"merged": merged}


def _events_are_current(conn, events: list[dict[str, Any]]) -> bool:
    for event in events:
        row = conn.execute(
            """SELECT updated_at, status, current_step_id
               FROM event_threads WHERE id = ?""",
            (int(event.get("id") or 0),),
        ).fetchone()
        if not row:
            return False
        if (
            str(row["updated_at"] or "") != str(event.get("last_seen_at") or "")
            or str(row["status"] or "") != str(event.get("status") or "")
            or int(row["current_step_id"] or 0)
            != int(event.get("current_step_id") or 0)
        ):
            return False
    return True


def _plan_event_rollup(
    *,
    snapshot: dict,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    steps = list(event.get("maintenance_steps") or [])
    if len(steps) <= EVENT_ROLLUP_MIN_STEPS:
        return None
    compact_steps = steps[:-EVENT_ROLLUP_RECENT_STEPS][:EVENT_ROLLUP_BATCH_STEPS]
    if not compact_steps:
        return None
    through_step_id = int(compact_steps[-1]["id"])
    result = call_maintenance_model_json(
        EVENT_THREAD_ROLLUP_PROMPT,
        {
            "session_id": snapshot["session_id"],
            "event_thread": _event_for_model(event),
            "previous_rollup": event.get("rollup_summary") or "",
            "through_step_id": through_step_id,
            "event_steps": compact_steps,
        },
        max_tokens=1800,
        task_name="event_thread_rollup_maintenance",
    )
    if _int(result.get("through_step_id")) != through_step_id:
        return None
    summary = str(result.get("summary") or "").strip()
    confidence = _confidence(result.get("confidence"))
    if not summary or confidence < 0.8:
        return None
    return {
        "thread_id": int(event["id"]),
        "through_step_id": through_step_id,
        "summary": summary,
        "open_loops": str(result.get("open_loops") or "").strip(),
        "stable_outcomes": str(result.get("stable_outcomes") or "").strip(),
        "confidence": confidence,
        "notes": str(result.get("notes") or "").strip(),
        "evidence_step_ids": [int(step["id"]) for step in compact_steps],
        "before": {
            "rollup_summary": event.get("rollup_summary") or "",
            "compacted_through_step_id": event.get("compacted_through_step_id"),
        },
    }


def _apply_event_rollup_plan(
    conn,
    *,
    snapshot: dict,
    plan: dict[str, Any],
    apply: bool,
    run_date: str,
    trigger: str,
) -> int:
    if not apply:
        return 1
    changed = apply_event_thread_rollup(
        conn,
        snapshot["session_id"],
        thread_id=plan["thread_id"],
        through_step_id=plan["through_step_id"],
        summary=plan["summary"],
        open_loops=plan["open_loops"],
        stable_outcomes=plan["stable_outcomes"],
        now=datetime.now().isoformat(),
    )
    if not changed:
        return 0
    record_memory_mutation(
        conn,
        run_date=run_date,
        trigger=trigger,
        session_id=snapshot["session_id"],
        entity_type="event_thread",
        entity_id=plan["thread_id"],
        action="rollup",
        before=plan["before"],
        after={
            "rollup_summary": plan["summary"],
            "compacted_through_step_id": plan["through_step_id"],
            "open_loops": plan["open_loops"],
            "stable_outcomes": plan["stable_outcomes"],
        },
        evidence=plan["evidence_step_ids"],
        reason=plan["notes"],
        confidence=plan["confidence"],
    )
    return 1


def _event_for_model(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"maintenance_steps", "search_text"}
    }


def _event_by_id(events: list[dict[str, Any]], event_id: int) -> dict[str, Any]:
    for event in events:
        if int(event.get("id") or 0) == int(event_id):
            return _event_for_model(event)
    return {}


def _event_merge_candidates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, first in enumerate(events):
        first_people = _event_people(first)
        for second in events[index + 1 :]:
            if not _events_can_merge(first, second, first_people):
                continue
            similarity = _event_similarity(first, second)
            same_title = (
                _text(first.get("title")).lower() == _text(second.get("title")).lower()
            )
            if not same_title and similarity < 0.35:
                continue
            candidates.append(
                {
                    "candidate_ids": [int(first["id"]), int(second["id"])],
                    "similarity": round(similarity, 3),
                    "events": [
                        _event_for_model(first),
                        _event_for_model(second),
                    ],
                }
            )
    candidates.sort(key=lambda item: (-item["similarity"], item["candidate_ids"]))
    return candidates[:40]


def _events_can_merge(
    first: dict[str, Any],
    second: dict[str, Any],
    first_people: set[str],
) -> bool:
    first_time = _text(first.get("event_time"))
    second_time = _text(second.get("event_time"))
    if first_time and second_time and first_time != second_time:
        return False
    first_kind = _text(first.get("kind"))
    second_kind = _text(second.get("kind"))
    if first_kind and second_kind and first_kind != second_kind:
        return False
    first_task = first.get("linked_task_id")
    second_task = second.get("linked_task_id")
    if first_task and second_task and int(first_task) != int(second_task):
        return False
    second_people = _event_people(second)
    return not (first_people and second_people and not (first_people & second_people))


def _event_people(event: dict[str, Any]) -> set[str]:
    return {
        str(person.get("person_key") or "")
        for person in event.get("people") or []
        if isinstance(person, dict)
        and person.get("person_key")
        and person.get("person_key") != INSTANCE_PERSON_KEY
    }


def _event_similarity(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_tokens = _memory_tokens(
        " ".join(
            _text(first.get(key))
            for key in ("title", "details", "followup_hint", "merge_hint")
        )
    )
    second_tokens = _memory_tokens(
        " ".join(
            _text(second.get(key))
            for key in ("title", "details", "followup_hint", "merge_hint")
        )
    )
    if not first_tokens or not second_tokens:
        return 0.0
    return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)


def _normalize_event_thread_merges(
    values: Any,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    allowed_pairs = {
        frozenset(int(item) for item in candidate["candidate_ids"])
        for candidate in candidates
    }
    out: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        canonical_id = _int(item.get("canonical_id"))
        source_ids = _normalize_int_list(
            item.get("source_ids", []),
            {
                event_id
                for pair in allowed_pairs
                for event_id in pair
                if event_id != canonical_id
            },
        )
        source_ids = [
            source_id
            for source_id in source_ids
            if frozenset((canonical_id, source_id)) in allowed_pairs
        ]
        confidence = _confidence(item.get("confidence"))
        if (
            not source_ids
            or confidence < 0.9
            or canonical_id in consumed
            or any(source_id in consumed for source_id in source_ids)
        ):
            continue
        consumed.update([canonical_id, *source_ids])
        out.append(
            {
                "canonical_id": canonical_id,
                "source_ids": source_ids,
                "title": _text(item.get("title")),
                "details": _text(item.get("details")),
                "followup_hint": _text(item.get("followup_hint")),
                "confidence": confidence,
                "reason": _text(item.get("reason")),
            }
        )
    return out


def _normalize_event_thread_updates(
    values: Any,
    allowed_ids: set[int],
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        event_id = _int(item.get("id"))
        if event_id not in allowed_ids or event_id in seen:
            continue
        confidence = (
            _confidence(item.get("confidence"))
            if item.get("confidence") is not None
            else None
        )
        if confidence is None or confidence < 0.8:
            continue
        seen.add(event_id)
        out.append(
            {
                "id": event_id,
                "title": _text(item.get("title")),
                "kind": _text(item.get("kind")),
                "event_time": _text(item.get("event_time")),
                "time_text": _text(item.get("time_text")),
                "details": _text(item.get("details")),
                "followup_hint": _text(item.get("followup_hint")),
                "confidence": confidence,
            }
        )
    return out


def _memory_tokens(value: object) -> set[str]:
    text = str(value or "").lower()
    tokens = {
        token
        for token in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", text)
        if len(token) >= 2
    }
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
        tokens.update(chunk[index : index + 3] for index in range(len(chunk) - 2))
    return tokens


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return str(value or "").strip()
