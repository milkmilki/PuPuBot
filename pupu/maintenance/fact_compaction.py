"""Evidence-preserving consolidation for person and relationship facts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..storage.people import INSTANCE_PERSON_KEY
from .audit import record_memory_mutation
from .model_call import call_maintenance_model_json
from .prompt import FACTS_MAINTENANCE_PROMPT
from .snapshot import _normalize_int_list

FACT_CHUNK_SIZE = 30


def run_fact_compaction(
    conn,
    snapshot: dict,
    *,
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict:
    facts = [item for item in snapshot.get("facts", []) if isinstance(item, dict)]
    if not facts:
        return _empty_result()

    archived = 0
    updated = 0
    conflicts = 0
    notes: list[str] = []
    planned_actions: list[dict[str, Any]] = []
    for index in range(0, len(facts), FACT_CHUNK_SIZE):
        chunk = facts[index : index + FACT_CHUNK_SIZE]
        merge_candidates = _fact_merge_candidates(chunk)
        result = call_maintenance_model_json(
            FACTS_MAINTENANCE_PROMPT,
            {
                "session_id": snapshot["session_id"],
                "facts": chunk,
                "merge_candidates": merge_candidates,
            },
            max_tokens=5000,
            task_name="facts_maintenance",
        )
        raw_actions = result.get("actions", [])
        if not raw_actions:
            raw_actions = _legacy_fact_actions(result, chunk)
        actions = _normalize_fact_actions(
            raw_actions,
            chunk,
            merge_candidates=merge_candidates,
        )
        note = str(result.get("notes", "")).strip()
        if note:
            notes.append(note)
        planned_actions.extend(actions)

    for action in planned_actions:
        outcome = _apply_fact_action(
            conn,
            session_id=snapshot["session_id"],
            action=action,
            facts=facts,
            apply=apply,
            run_date=run_date,
            trigger=trigger,
        )
        archived += outcome["archived"]
        updated += outcome["updated"]
        conflicts += outcome["conflicts"]

    return {
        "deleted_facts": archived,
        "updated_facts": updated,
        "fact_conflicts": conflicts,
        "note": " | ".join(notes[:4]),
    }


def _empty_result() -> dict:
    return {
        "deleted_facts": 0,
        "updated_facts": 0,
        "fact_conflicts": 0,
        "note": "",
    }


def _legacy_fact_actions(result: dict, facts: list[dict[str, Any]]) -> list[dict]:
    actions: list[dict] = []
    groups = (
        (
            INSTANCE_PERSON_KEY,
            result.get("instance_updates", {}),
            result.get("instance_delete_keys", []),
        ),
    )
    owner_keys = {
        str(fact.get("subject_person_key") or "")
        for fact in facts
        if str(fact.get("subject_person_key") or "") != INSTANCE_PERSON_KEY
    }
    if len(owner_keys) == 1:
        groups = (
            (
                next(iter(owner_keys)),
                result.get("owner_updates", {}),
                result.get("owner_delete_keys", []),
            ),
            *groups,
        )
    for subject_key, updates, delete_keys in groups:
        by_key = {
            str(fact.get("fact_key") or ""): fact
            for fact in facts
            if fact.get("subject_person_key") == subject_key
            and not fact.get("object_person_key")
            and fact.get("scope") == "person"
        }
        if isinstance(updates, dict):
            for key, value in updates.items():
                fact = by_key.get(str(key))
                if fact and str(value).strip():
                    actions.append(
                        {
                            "action": "rewrite",
                            "fact_id": fact["id"],
                            "value": str(value).strip(),
                            "confidence": 1.0,
                            "evidence_fact_ids": [fact["id"]],
                            "reason": "兼容旧版 maintenance 输出",
                        }
                    )
        if isinstance(delete_keys, list):
            for key in delete_keys:
                fact = by_key.get(str(key))
                if fact:
                    actions.append(
                        {
                            "action": "archive",
                            "fact_id": fact["id"],
                            "confidence": 1.0,
                            "evidence_fact_ids": [fact["id"]],
                            "reason": "兼容旧版 maintenance 输出",
                        }
                    )
    return actions


def _normalize_fact_actions(
    values: Any,
    facts: list[dict[str, Any]],
    *,
    merge_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    by_id = {int(fact["id"]): fact for fact in facts}
    allowed_merge_pairs = {
        frozenset(int(value) for value in candidate["candidate_ids"])
        for candidate in merge_candidates
    }
    out: list[dict[str, Any]] = []
    claimed_ids: set[int] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        confidence = _confidence(item.get("confidence"))
        reason = str(item.get("reason") or "").strip()
        if action in {"rewrite", "archive", "conflict"}:
            fact_id = _int(item.get("fact_id"))
            if fact_id not in by_id or fact_id in claimed_ids:
                continue
            value = str(item.get("value") or "").strip()
            if action == "rewrite" and (not value or confidence < 0.8):
                continue
            if action == "archive" and confidence < 0.9:
                continue
            evidence_ids = _normalize_int_list(
                item.get("evidence_fact_ids", []),
                set(by_id),
            ) or [fact_id]
            out.append(
                {
                    "action": action,
                    "fact_id": fact_id,
                    "value": value,
                    "confidence": confidence,
                    "evidence_fact_ids": evidence_ids,
                    "reason": reason,
                }
            )
            claimed_ids.add(fact_id)
            continue
        if action != "merge" or confidence < 0.9:
            continue
        target_id = _int(item.get("target_fact_id"))
        if target_id not in by_id:
            continue
        source_ids = [
            source_id
            for source_id in _normalize_int_list(
                item.get("source_fact_ids", []),
                set(by_id),
            )
            if source_id != target_id and source_id not in claimed_ids
        ]
        if target_id in claimed_ids:
            continue
        target = by_id[target_id]
        source_ids = [
            source_id
            for source_id in source_ids
            if _fact_scope_signature(by_id[source_id]) == _fact_scope_signature(target)
            and frozenset((target_id, source_id)) in allowed_merge_pairs
        ]
        value = str(item.get("value") or "").strip()
        if not source_ids or not value:
            continue
        evidence_ids = _normalize_int_list(
            item.get("evidence_fact_ids", []),
            set(by_id),
        ) or [target_id, *source_ids]
        claimed_ids.update([target_id, *source_ids])
        out.append(
            {
                "action": "merge",
                "target_fact_id": target_id,
                "source_fact_ids": source_ids,
                "value": value,
                "confidence": confidence,
                "evidence_fact_ids": evidence_ids,
                "reason": reason,
            }
        )
    return out


def _fact_merge_candidates(
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, first in enumerate(facts):
        for second in facts[index + 1 :]:
            if _fact_scope_signature(first) != _fact_scope_signature(second):
                continue
            first_tokens = _memory_tokens(
                f"{first.get('fact_key') or ''} {first.get('fact_value') or ''}"
            )
            second_tokens = _memory_tokens(
                f"{second.get('fact_key') or ''} {second.get('fact_value') or ''}"
            )
            similarity = (
                len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
                if first_tokens and second_tokens
                else 0.0
            )
            key_overlap = bool(
                _memory_tokens(first.get("fact_key"))
                & _memory_tokens(second.get("fact_key"))
            )
            if similarity < 0.3 and not key_overlap:
                continue
            candidates.append(
                {
                    "candidate_ids": [int(first["id"]), int(second["id"])],
                    "similarity": round(similarity, 3),
                }
            )
    candidates.sort(key=lambda item: (-item["similarity"], item["candidate_ids"]))
    return candidates[:60]


def _fact_scope_signature(fact: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(fact.get("subject_person_key") or ""),
        str(fact.get("object_person_key") or ""),
        str(fact.get("scope") or "person"),
    )


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


def _apply_fact_action(
    conn,
    *,
    session_id: str,
    action: dict[str, Any],
    facts: list[dict[str, Any]],
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict[str, int]:
    by_id = {int(fact["id"]): fact for fact in facts}
    action_name = action["action"]
    confidence = action["confidence"]
    evidence_ids = action["evidence_fact_ids"]
    reason = action["reason"]
    outcome = {"archived": 0, "updated": 0, "conflicts": 0}
    touched_ids = (
        [action["target_fact_id"], *action["source_fact_ids"]]
        if action_name == "merge"
        else [action["fact_id"]]
    )
    if apply and any(
        not _fact_is_current(conn, by_id[fact_id])
        for fact_id in touched_ids
    ):
        return outcome
    if action_name == "conflict":
        outcome["conflicts"] = 1
        if apply:
            fact = by_id[action["fact_id"]]
            record_memory_mutation(
                conn,
                run_date=run_date,
                trigger=trigger,
                session_id=session_id,
                entity_type="person_fact",
                entity_id=fact["id"],
                action="conflict",
                before=fact,
                after=fact,
                evidence=evidence_ids,
                reason=reason,
                confidence=confidence,
                status="conflict",
            )
        return outcome
    if action_name == "merge":
        return _apply_fact_merge(
            conn,
            session_id=session_id,
            action=action,
            by_id=by_id,
            apply=apply,
            run_date=run_date,
            trigger=trigger,
        )
    fact = by_id[action["fact_id"]]
    if action_name == "rewrite":
        return _apply_fact_rewrite(
            conn,
            session_id=session_id,
            action=action,
            fact=fact,
            apply=apply,
            run_date=run_date,
            trigger=trigger,
        )
    return _apply_fact_archive(
        conn,
        session_id=session_id,
        action=action,
        fact=fact,
        apply=apply,
        run_date=run_date,
        trigger=trigger,
    )


def _fact_is_current(conn, fact: dict[str, Any]) -> bool:
    row = conn.execute(
        """SELECT fact_value, status, updated_at FROM person_facts
           WHERE id = ?""",
        (int(fact["id"]),),
    ).fetchone()
    return bool(
        row
        and row["fact_value"] == fact.get("fact_value")
        and row["status"] == fact.get("status")
        and row["updated_at"] == fact.get("updated_at")
    )


def _apply_fact_merge(
    conn,
    *,
    session_id: str,
    action: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict[str, int]:
    target = by_id[action["target_fact_id"]]
    sources = [by_id[source_id] for source_id in action["source_fact_ids"]]
    before = {"target": target, "sources": sources}
    after = {
        "target_fact_id": target["id"],
        "source_fact_ids": action["source_fact_ids"],
        "value": action["value"],
    }
    outcome = {
        "archived": len(sources),
        "updated": 1,
        "conflicts": 0,
    }
    if not apply:
        return outcome
    now = datetime.now().isoformat()
    conn.execute(
        """UPDATE person_facts
           SET fact_value = ?, confidence = ?, updated_at = ?
           WHERE id = ? AND status = 'active'""",
        (
            action["value"],
            max(
                [float(target.get("confidence") or 0.0)]
                + [float(source.get("confidence") or 0.0) for source in sources]
            ),
            now,
            int(target["id"]),
        ),
    )
    for source in sources:
        _copy_fact_evidence(conn, int(source["id"]), int(target["id"]))
        conn.execute(
            """UPDATE person_facts
               SET status = 'superseded', superseded_by_id = ?, updated_at = ?
               WHERE id = ?""",
            (int(target["id"]), now, int(source["id"])),
        )
    record_memory_mutation(
        conn,
        run_date=run_date,
        trigger=trigger,
        session_id=session_id,
        entity_type="person_fact",
        entity_id=target["id"],
        action="merge",
        before=before,
        after=after,
        evidence=action["evidence_fact_ids"],
        reason=action["reason"],
        confidence=action["confidence"],
    )
    return outcome


def _copy_fact_evidence(conn, source_id: int, target_id: int) -> None:
    conn.execute(
        """INSERT INTO fact_evidence (
               fact_id, source_context_session, source_msg_start_id,
               source_msg_end_id, evidence_text, created_at
           )
           SELECT ?, fe.source_context_session, fe.source_msg_start_id,
                  fe.source_msg_end_id, fe.evidence_text, fe.created_at
           FROM fact_evidence fe
           WHERE fe.fact_id = ?
             AND NOT EXISTS (
                 SELECT 1 FROM fact_evidence existing
                 WHERE existing.fact_id = ?
                   AND existing.source_context_session = fe.source_context_session
                   AND COALESCE(existing.source_msg_start_id, -1) =
                       COALESCE(fe.source_msg_start_id, -1)
                   AND COALESCE(existing.source_msg_end_id, -1) =
                       COALESCE(fe.source_msg_end_id, -1)
                   AND existing.evidence_text = fe.evidence_text
             )""",
        (target_id, source_id, target_id),
    )


def _apply_fact_rewrite(
    conn,
    *,
    session_id: str,
    action: dict[str, Any],
    fact: dict[str, Any],
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict[str, int]:
    empty = {"archived": 0, "updated": 0, "conflicts": 0}
    if action["value"] == str(fact.get("fact_value") or "").strip():
        return empty
    if not apply:
        return {**empty, "updated": 1}
    after = {**fact, "fact_value": action["value"]}
    conn.execute(
        """UPDATE person_facts
           SET fact_value = ?, updated_at = ?
           WHERE id = ? AND status = 'active'""",
        (action["value"], datetime.now().isoformat(), int(fact["id"])),
    )
    record_memory_mutation(
        conn,
        run_date=run_date,
        trigger=trigger,
        session_id=session_id,
        entity_type="person_fact",
        entity_id=fact["id"],
        action="rewrite",
        before=fact,
        after=after,
        evidence=action["evidence_fact_ids"],
        reason=action["reason"],
        confidence=action["confidence"],
    )
    return {**empty, "updated": 1}


def _apply_fact_archive(
    conn,
    *,
    session_id: str,
    action: dict[str, Any],
    fact: dict[str, Any],
    apply: bool,
    run_date: str,
    trigger: str,
) -> dict[str, int]:
    empty = {"archived": 0, "updated": 0, "conflicts": 0}
    after = {**fact, "status": "archived"}
    if not apply:
        return {**empty, "archived": 1}
    conn.execute(
        """UPDATE person_facts
           SET status = 'archived', superseded_by_id = NULL, updated_at = ?
           WHERE id = ? AND status = 'active'""",
        (datetime.now().isoformat(), int(fact["id"])),
    )
    record_memory_mutation(
        conn,
        run_date=run_date,
        trigger=trigger,
        session_id=session_id,
        entity_type="person_fact",
        entity_id=fact["id"],
        action="archive",
        before=fact,
        after=after,
        evidence=action["evidence_fact_ids"],
        reason=action["reason"],
        confidence=action["confidence"],
    )
    return {**empty, "archived": 1}


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
