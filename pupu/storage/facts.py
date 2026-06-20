"""Persistence helpers for person-scoped long-term facts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .db import get_conn
from .people import (
    INSTANCE_PERSON_KEY,
    OWNER_PERSON_KEY,
    default_instance_person,
    default_owner_person,
    ensure_default_people,
    normalize_person_key,
    upsert_person,
)

VALID_FACT_SCOPES = {"person", "relationship"}


def _now() -> str:
    return datetime.now().isoformat()


def _text(value: Any, fallback: str = "") -> str:
    return str(value if value is not None else fallback).strip()


def _fact_scalar(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return None
    return str(value).strip()


def _clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 1.0
    return max(0.0, min(1.0, number))


def _subject_label(person: dict[str, Any]) -> str:
    return _text(person.get("display_name")) or _text(person.get("person_key"))


def _fact_row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "subject_person_key": row["subject_person_key"],
        "object_person_key": row["object_person_key"],
        "scope": row["scope"],
        "legacy_session_id": row["legacy_session_id"],
        "fact_key": row["fact_key"],
        "fact_value": row["fact_value"],
        "confidence": float(row["confidence"] or 0.0),
        "source_context_session": row["source_context_session"],
        "source_msg_start_id": row["source_msg_start_id"],
        "source_msg_end_id": row["source_msg_end_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "subject_display_name": row["subject_display_name"] or row["subject_person_key"],
        "object_display_name": row["object_display_name"] or row["object_person_key"],
    }


def _known_people_map(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT person_key, display_name, aliases FROM people"
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        key = normalize_person_key(row["person_key"])
        display = _text(row["display_name"])
        if key:
            out[key] = key
        if display:
            out[display] = key
        aliases = row["aliases"]
        if aliases:
            try:
                parsed = json.loads(aliases)
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                for alias in parsed:
                    alias_text = _text(alias)
                    if alias_text:
                        out[alias_text] = key
    out.setdefault("用户", OWNER_PERSON_KEY)
    out.setdefault("我", OWNER_PERSON_KEY)
    out.setdefault("自己", OWNER_PERSON_KEY)
    out.setdefault("实例", INSTANCE_PERSON_KEY)
    out.setdefault("仆仆", INSTANCE_PERSON_KEY)
    out.setdefault("pupu", INSTANCE_PERSON_KEY)
    return out


def resolve_person_reference(
    conn,
    value: Any,
    *,
    default_key: str = "",
    known_people: list[dict[str, Any]] | None = None,
) -> str:
    raw = _text(value)
    if not raw:
        return normalize_person_key(default_key)

    normalized = normalize_person_key(raw)
    if normalized in {OWNER_PERSON_KEY, INSTANCE_PERSON_KEY} or normalized.startswith(("qq:", "qqofficial:")):
        return normalized

    lookup = _known_people_map(conn)
    for person in known_people or []:
        if not isinstance(person, dict):
            continue
        key = normalize_person_key(person.get("person_key"))
        if not key:
            continue
        name = _text(person.get("display_name"))
        if name:
            lookup[name] = key
        aliases = person.get("aliases")
        if isinstance(aliases, str):
            alias_values = [aliases]
        elif isinstance(aliases, (list, tuple, set)):
            alias_values = list(aliases)
        else:
            alias_values = []
        for alias in alias_values:
            alias_text = _text(alias)
            if alias_text:
                lookup[alias_text] = key

    if raw in lookup:
        return lookup[raw]
    return normalized or normalize_person_key(default_key)


def upsert_person_facts(
    facts: list[dict[str, Any]] | dict[str, str],
    *,
    default_subject_person_key: str = OWNER_PERSON_KEY,
    legacy_session_id: str = "",
    known_people: list[dict[str, Any]] | None = None,
    context_session: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
) -> list[dict[str, Any]]:
    conn = get_conn()
    now = _now()
    saved: list[dict[str, Any]] = []
    try:
        ensure_default_people(conn, now=now)
        owner = default_owner_person()
        upsert_person(
            conn,
            OWNER_PERSON_KEY,
            kind=owner["kind"],
            display_name=owner["display_name"],
            now=now,
        )
        instance = default_instance_person()
        upsert_person(
            conn,
            INSTANCE_PERSON_KEY,
            kind=instance["kind"],
            display_name=instance["display_name"],
            now=now,
        )
        for person in known_people or []:
            if not isinstance(person, dict):
                continue
            person_key = normalize_person_key(person.get("person_key"))
            if not person_key:
                continue
            upsert_person(
                conn,
                person_key,
                kind=_text(person.get("kind")),
                display_name=_text(person.get("display_name")),
                qq_id=_text(person.get("qq_id")),
                aliases=person.get("aliases"),
                now=now,
            )

        if isinstance(facts, dict):
            raw_items: list[dict[str, Any]] = [
                {
                    "subject": default_subject_person_key,
                    "key": key,
                    "value": value,
                    "scope": "person",
                }
                for key, value in facts.items()
            ]
        elif isinstance(facts, list):
            raw_items = [item for item in facts if isinstance(item, dict)]
        else:
            raw_items = []

        for item in raw_items:
            key = _fact_scalar(item.get("key") or item.get("fact_key"))
            value = _fact_scalar(item.get("value") or item.get("fact_value"))
            if not key:
                continue
            if value is None:
                continue

            subject_ref = item.get("subject") or item.get("subject_person_key") or default_subject_person_key
            subject_key = resolve_person_reference(
                conn,
                subject_ref,
                default_key=default_subject_person_key,
                known_people=known_people,
            )
            if not subject_key:
                continue

            object_ref = item.get("object") or item.get("object_person_key") or ""
            object_key = resolve_person_reference(
                conn,
                object_ref,
                default_key="",
                known_people=known_people,
            )
            scope = _text(item.get("scope") or "person").lower()
            if scope not in VALID_FACT_SCOPES:
                scope = "relationship" if object_key else "person"
            if object_key and scope == "person":
                scope = "relationship"

            if not value:
                conn.execute(
                    """DELETE FROM person_facts
                       WHERE subject_person_key = ? AND object_person_key = ?
                         AND scope = ? AND fact_key = ?""",
                    (subject_key, object_key, scope, key),
                )
                continue

            conn.execute(
                """INSERT INTO person_facts (
                       subject_person_key, object_person_key, scope, legacy_session_id,
                       fact_key, fact_value, confidence, source_context_session,
                       source_msg_start_id, source_msg_end_id, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subject_person_key, object_person_key, scope, fact_key)
                   DO UPDATE SET
                       fact_value = excluded.fact_value,
                       confidence = excluded.confidence,
                       legacy_session_id = CASE
                           WHEN excluded.legacy_session_id != '' THEN excluded.legacy_session_id
                           ELSE person_facts.legacy_session_id
                       END,
                       source_context_session = excluded.source_context_session,
                       source_msg_start_id = excluded.source_msg_start_id,
                       source_msg_end_id = excluded.source_msg_end_id,
                       updated_at = excluded.updated_at""",
                (
                    subject_key,
                    object_key,
                    scope,
                    _text(item.get("legacy_session_id") or legacy_session_id),
                    key,
                    value,
                    _clamp_confidence(item.get("confidence", 1.0)),
                    _text(item.get("source_context_session") or context_session),
                    item.get("source_msg_start_id", source_msg_start_id),
                    item.get("source_msg_end_id", source_msg_end_id),
                    now,
                    now,
                ),
            )
            saved.append(
                {
                    "subject_person_key": subject_key,
                    "object_person_key": object_key,
                    "scope": scope,
                    "fact_key": key,
                    "fact_value": value,
                }
            )
        conn.commit()
    finally:
        conn.close()
    return saved


def get_person_facts(
    *,
    subject_person_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    include_relationships: bool = True,
) -> list[dict[str, Any]]:
    normalized_subjects = [
        key
        for key in (normalize_person_key(item) for item in (subject_person_keys or []))
        if key
    ]
    params: list[Any] = []
    where: list[str] = []
    if normalized_subjects:
        placeholders = ",".join("?" for _ in normalized_subjects)
        if include_relationships:
            where.append(
                "("
                f"(pf.object_person_key = '' AND pf.subject_person_key IN ({placeholders})) "
                "OR "
                f"(pf.object_person_key != '' AND "
                f"(pf.subject_person_key IN ({placeholders}) OR pf.object_person_key IN ({placeholders})))"
                ")"
            )
            params.extend(normalized_subjects)
            params.extend(normalized_subjects)
            params.extend(normalized_subjects)
        else:
            where.append(f"pf.subject_person_key IN ({placeholders})")
            params.extend(normalized_subjects)
    sql = """
        SELECT pf.*,
               sp.display_name AS subject_display_name,
               op.display_name AS object_display_name
        FROM person_facts pf
        LEFT JOIN people sp ON sp.person_key = pf.subject_person_key
        LEFT JOIN people op ON op.person_key = pf.object_person_key
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pf.scope ASC, pf.subject_person_key ASC, pf.object_person_key ASC, pf.updated_at ASC"
    conn = get_conn()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [_fact_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def get_person_fact_map(subject_person_key: str) -> dict[str, str]:
    subject_key = normalize_person_key(subject_person_key)
    if not subject_key:
        return {}
    rows = get_person_facts(subject_person_keys=[subject_key], include_relationships=False)
    return {
        row["fact_key"]: row["fact_value"]
        for row in rows
        if row["subject_person_key"] == subject_key
        and not row["object_person_key"]
        and row["scope"] == "person"
    }


def update_person_fact_by_id(
    fact_id: int,
    *,
    value: str,
    confidence: Any = None,
    context_session: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
) -> dict[str, Any] | None:
    fact_value = _fact_scalar(value)
    if not fact_value:
        return None
    now = _now()
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT pf.*,
                      sp.display_name AS subject_display_name,
                      op.display_name AS object_display_name
               FROM person_facts pf
               LEFT JOIN people sp ON sp.person_key = pf.subject_person_key
               LEFT JOIN people op ON op.person_key = pf.object_person_key
               WHERE pf.id = ?""",
            (int(fact_id),),
        ).fetchone()
        if not row:
            return None
        next_confidence = (
            float(row["confidence"] or 0.0)
            if confidence is None
            else _clamp_confidence(confidence)
        )
        conn.execute(
            """UPDATE person_facts
               SET fact_value = ?,
                   confidence = ?,
                   source_context_session = ?,
                   source_msg_start_id = ?,
                   source_msg_end_id = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                fact_value,
                next_confidence,
                _text(context_session),
                source_msg_start_id,
                source_msg_end_id,
                now,
                int(fact_id),
            ),
        )
        conn.commit()
        updated = conn.execute(
            """SELECT pf.*,
                      sp.display_name AS subject_display_name,
                      op.display_name AS object_display_name
               FROM person_facts pf
               LEFT JOIN people sp ON sp.person_key = pf.subject_person_key
               LEFT JOIN people op ON op.person_key = pf.object_person_key
               WHERE pf.id = ?""",
            (int(fact_id),),
        ).fetchone()
        return _fact_row_to_dict(updated) if updated else None
    finally:
        conn.close()


def format_person_fact_subject(row: dict[str, Any]) -> str:
    subject = _text(row.get("subject_display_name")) or _text(row.get("subject_person_key"))
    object_name = _text(row.get("object_display_name")) or _text(row.get("object_person_key"))
    scope = _text(row.get("scope") or "person")
    if scope == "relationship" and object_name:
        return f"{subject} ↔ {object_name}"
    return subject


def group_person_facts_for_display(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        label = format_person_fact_subject(row)
        if label not in grouped:
            grouped[label] = []
            order.append(label)
        grouped[label].append(row)
    return [(label, grouped[label]) for label in order]
