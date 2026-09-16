"""Auditable mutation helpers for memory consolidation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def mutation_fingerprint(
    *,
    session_id: str,
    entity_type: str,
    entity_id: object,
    action: str,
    after: Any,
) -> str:
    fingerprint_after = after
    if action == "merge" and isinstance(after, dict):
        fingerprint_after = {
            key: after.get(key)
            for key in (
                "canonical_id",
                "source_ids",
                "target_fact_id",
                "source_fact_ids",
            )
            if key in after
        }
    elif action == "archive":
        fingerprint_after = {"status": "archived"}
    payload = _json(
        {
            "session_id": session_id,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "action": action,
            "after": fingerprint_after,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_memory_mutation(
    conn,
    *,
    run_date: str,
    trigger: str,
    session_id: str,
    entity_type: str,
    entity_id: object,
    action: str,
    before: Any,
    after: Any,
    evidence: Any = None,
    reason: str = "",
    confidence: float = 0.0,
    status: str = "applied",
) -> str:
    fingerprint = mutation_fingerprint(
        session_id=session_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        after=after,
    )
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO memory_mutations (
               run_date, trigger, session_id, entity_type, entity_id, action,
               before_json, after_json, evidence_json, reason, confidence,
               fingerprint, status, created_at, applied_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_date,
            trigger,
            session_id,
            entity_type,
            str(entity_id),
            action,
            _json(before),
            _json(after),
            _json(evidence if evidence is not None else []),
            str(reason or "").strip(),
            max(0.0, min(1.0, float(confidence or 0.0))),
            fingerprint,
            status,
            now,
            now if status == "applied" else None,
        ),
    )
    return fingerprint
