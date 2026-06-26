"""Project SQLite memory sources into semantic index cards."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, unquote

from ..persona.core import get_pupu_name
from ..storage.db import get_conn
from ..storage.event_threads import get_recent_event_threads
from ..storage.facts import get_person_facts
from .config import semantic_source_summary_limit

SOURCE_PROJECTION_KIND = "rag_card"
SOURCE_BACKED_KINDS = {"summary", "person_fact", "event_thread"}


def _text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _short_hash(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def source_version(*values: object) -> str:
    return _short_hash([value for value in values])


def summary_source_key(context_session: str, start_msg_id: int, end_msg_id: int) -> str:
    return f"summary:{quote(str(context_session or ''), safe='')}:{int(start_msg_id or 0)}:{int(end_msg_id or 0)}"


def person_fact_source_key(fact: dict[str, Any]) -> str:
    return ":".join(
        (
            "person_fact",
            quote(str(fact.get("subject_person_key") or fact.get("subject") or "").strip(), safe=""),
            quote(str(fact.get("object_person_key") or fact.get("object") or "").strip(), safe=""),
            quote(str(fact.get("scope") or "person").strip(), safe=""),
            quote(str(fact.get("fact_key") or fact.get("key") or "").strip(), safe=""),
        )
    )


def event_thread_source_key(event: dict[str, Any]) -> str:
    return ":".join(
        (
            "event_thread",
            quote(str(event.get("session_id") or "").strip(), safe=""),
            quote(str(event.get("thread_key") or event.get("key") or "").strip(), safe=""),
        )
    )


def summary_source_version(summary: dict[str, Any]) -> str:
    return source_version(
        summary.get("session_id"),
        summary.get("summary"),
        summary.get("start_msg_id"),
        summary.get("end_msg_id"),
    )


def person_fact_source_version(fact: dict[str, Any]) -> str:
    return source_version(
        fact.get("id"),
        fact.get("subject_person_key"),
        fact.get("object_person_key"),
        fact.get("scope"),
        fact.get("fact_key"),
        fact.get("fact_value"),
        fact.get("confidence"),
        fact.get("updated_at"),
    )


def event_thread_source_version(event: dict[str, Any]) -> str:
    return source_version(
        event.get("id"),
        event.get("session_id"),
        event.get("thread_key") or event.get("key"),
        event.get("title"),
        event.get("status"),
        event.get("event_time"),
        event.get("details"),
        event.get("current_summary"),
        event.get("current_cause"),
        event.get("current_reflection"),
        event.get("followup_hint"),
        event.get("confidence"),
        event.get("last_seen_at") or event.get("updated_at"),
    )


def _parse_event_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _event_date_label(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def build_review_entries(
    *,
    summary: str,
    person_facts: list[dict] | None = None,
    event_threads: list[dict] | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    entries: list[tuple[str, str, dict[str, Any]]] = []
    character_name = get_pupu_name().strip() or "PuPu"
    summary_text = _text(summary)
    if summary_text:
        entries.append(("summary", f"对话摘要（用户 / {character_name}）: {summary_text}", {}))

    for fact in person_facts or []:
        if not isinstance(fact, dict):
            continue
        subject = _text(
            fact.get("subject_display_name")
            or fact.get("subject")
            or fact.get("subject_person_key")
        )
        obj = _text(
            fact.get("object_display_name")
            or fact.get("object")
            or fact.get("object_person_key")
        )
        key = _text(fact.get("fact_key") or fact.get("key"))
        value = _text(fact.get("fact_value") or fact.get("value"))
        scope = _text(fact.get("scope") or "person")
        if not key or not value:
            continue
        label = subject or "相关人物"
        if scope == "relationship" and obj:
            label = f"{label} -> {obj}"
        entries.append(
            (
                "person_fact",
                f"{label} | {key}: {value}",
                {
                    "key": key,
                    "subject_person_key": fact.get("subject_person_key"),
                    "object_person_key": fact.get("object_person_key"),
                    "scope": scope,
                },
            )
        )

    for event in event_threads or []:
        if not isinstance(event, dict):
            continue
        event_date = _parse_event_date(event.get("event_time"))
        event_label = _event_date_label(event_date) if event_date else ""
        people_label = _text(event.get("people_label"))
        title = _text(event.get("title"))
        details = _text(event.get("details") or event.get("current_summary"))
        followup = _text(event.get("followup_hint"))
        text_parts = [part for part in (title, details, followup) if part]
        joined = " ".join(text_parts)
        if event_label and event_label not in joined:
            text_parts.insert(0, event_label)
        event_text = "; ".join(text_parts)
        if event_text:
            if not people_label:
                people_label = f"用户 / {character_name}"
            entries.append(
                (
                    "event_thread",
                    f"相关人物: {people_label}; {event_text}",
                    {
                        "thread_key": event.get("thread_key"),
                        "event_time": event.get("event_time"),
                        "confidence": event.get("confidence"),
                    },
                )
            )
    return entries


def source_payload(kind: str, text: str, source: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    source_id = source.get("id")
    source_key = ""
    version = ""
    if kind == "summary":
        source_key = summary_source_key(
            str(source.get("session_id") or ""),
            int(source.get("start_msg_id") or 0),
            int(source.get("end_msg_id") or 0),
        )
        version = summary_source_version(source)
    elif kind == "person_fact":
        source_key = person_fact_source_key(source)
        version = person_fact_source_version(source)
    elif kind == "event_thread":
        source_key = event_thread_source_key(source)
        version = event_thread_source_version(source)
    payload = {
        "kind": kind,
        "text": text,
        "source_type": kind,
        "source_id": source_id,
        "source_key": source_key,
        "source_version": version,
        "projection_kind": SOURCE_PROJECTION_KIND,
    }
    if extra:
        payload.update(extra)
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _summary_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": row["session_id"],
        "summary": row["summary"],
        "start_msg_id": int(row["start_msg_id"]),
        "end_msg_id": int(row["end_msg_id"]),
        "created_at": row["created_at"],
    }


def load_all_summaries() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        limit = semantic_source_summary_limit()
        if limit > 0:
            rows = conn.execute(
                """SELECT id, session_id, summary, start_msg_id, end_msg_id, created_at
                   FROM summaries ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return list(reversed([_summary_row_to_dict(row) for row in rows]))
        rows = conn.execute(
            """SELECT id, session_id, summary, start_msg_id, end_msg_id, created_at
               FROM summaries ORDER BY created_at ASC, id ASC"""
        ).fetchall()
        return [_summary_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def load_summary_by_source_key(source_key: str) -> dict[str, Any] | None:
    parts = str(source_key or "").split(":")
    if len(parts) < 4 or parts[0] != "summary":
        return None
    try:
        start_msg_id = int(parts[-2])
        end_msg_id = int(parts[-1])
    except Exception:
        return None
    context_session = unquote(":".join(parts[1:-2]))
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, session_id, summary, start_msg_id, end_msg_id, created_at
               FROM summaries
               WHERE session_id = ? AND start_msg_id = ? AND end_msg_id = ?
               ORDER BY id DESC LIMIT 1""",
            (context_session, start_msg_id, end_msg_id),
        ).fetchone()
        return _summary_row_to_dict(row) if row else None
    finally:
        conn.close()


def load_all_event_threads() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT session_id FROM event_threads ORDER BY session_id ASC").fetchall()
        session_ids = [str(row["session_id"] or "").strip() for row in rows if str(row["session_id"] or "").strip()]
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for session_id in session_ids:
        events.extend(get_recent_event_threads(session_id, limit=None))
    return events


def load_event_thread_by_source_key(source_key: str) -> dict[str, Any] | None:
    parts = str(source_key or "").split(":")
    if len(parts) < 3 or parts[0] != "event_thread":
        return None
    session_id = unquote(parts[1])
    thread_key = unquote(":".join(parts[2:]))
    for event in get_recent_event_threads(session_id, limit=None):
        if str(event.get("thread_key") or "") == thread_key:
            return event
    return None


def load_person_fact_by_source_key(source_key: str) -> dict[str, Any] | None:
    parts = str(source_key or "").split(":")
    if len(parts) < 5 or parts[0] != "person_fact":
        return None
    subject = unquote(parts[1])
    obj = unquote(parts[2])
    scope = unquote(parts[3])
    key = unquote(":".join(parts[4:]))
    for fact in get_person_facts(include_relationships=True):
        if (
            str(fact.get("subject_person_key") or "") == subject
            and str(fact.get("object_person_key") or "") == obj
            and str(fact.get("scope") or "") == scope
            and str(fact.get("fact_key") or "") == key
        ):
            return fact
    return None


def entry_from_source(kind: str, source: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    if kind == "summary":
        entries = build_review_entries(summary=str(source.get("summary") or ""))
    elif kind == "person_fact":
        entries = build_review_entries(summary="", person_facts=[source])
    elif kind == "event_thread":
        entries = build_review_entries(summary="", event_threads=[source])
    else:
        entries = []
    if not entries:
        return None
    entry_kind, text, extra = entries[0]
    return entry_kind, text, source_payload(entry_kind, text, source, extra)


def expected_source_entries(identity_session: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    entries: list[tuple[str, str, dict[str, Any]]] = []
    for summary in load_all_summaries():
        entry = entry_from_source("summary", summary)
        if entry:
            entries.append(entry)
    for fact in get_person_facts(include_relationships=True):
        entry = entry_from_source("person_fact", fact)
        if entry:
            entries.append(entry)
    for event in load_all_event_threads():
        entry = entry_from_source("event_thread", event)
        if entry:
            entries.append(entry)
    return dedupe_entries_by_source_key(entries)


def lookup_source_entry(source_type: str, source_key: str) -> tuple[str, str, dict[str, Any]] | None:
    if source_type == "summary":
        row = load_summary_by_source_key(source_key)
        return entry_from_source("summary", row) if row else None
    if source_type == "person_fact":
        row = load_person_fact_by_source_key(source_key)
        return entry_from_source("person_fact", row) if row else None
    if source_type == "event_thread":
        row = load_event_thread_by_source_key(source_key)
        return entry_from_source("event_thread", row) if row else None
    return None


def dedupe_entries_by_source_key(entries: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    positions: dict[str, int] = {}
    for entry in entries:
        source_key = str(entry[2].get("source_key") or "").strip()
        if source_key:
            position = positions.get(source_key)
            if position is not None:
                out[position] = entry
                continue
            positions[source_key] = len(out)
        out.append(entry)
    return out


def entries_with_source_metadata(
    entries: list[tuple[str, str, dict[str, Any]]],
    *,
    context_session: str,
    start_msg_id: int,
    end_msg_id: int,
    summary: str,
    person_facts: list[dict] | None,
    event_threads: list[dict] | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    facts = [item for item in (person_facts or []) if isinstance(item, dict)]
    events = [item for item in (event_threads or []) if isinstance(item, dict)]
    fact_index = 0
    event_index = 0
    for kind, text, extra in entries:
        source: dict[str, Any] = {}
        if kind == "summary":
            source = {
                "session_id": context_session,
                "summary": summary,
                "start_msg_id": start_msg_id,
                "end_msg_id": end_msg_id,
            }
            persisted = load_summary_by_source_key(summary_source_key(context_session, start_msg_id, end_msg_id))
            if persisted:
                source.update(persisted)
        elif kind == "person_fact":
            while fact_index < len(facts):
                candidate = facts[fact_index]
                fact_index += 1
                if _text(candidate.get("fact_key") or candidate.get("key")) and _text(
                    candidate.get("fact_value") or candidate.get("value")
                ):
                    source = candidate
                    break
        elif kind == "event_thread":
            while event_index < len(events):
                candidate = events[event_index]
                event_index += 1
                if _text(candidate.get("thread_key") or candidate.get("key")):
                    source = candidate
                    break
        payload_extra = dict(extra or {})
        if kind in SOURCE_BACKED_KINDS and source:
            payload_extra.update(source_payload(kind, text, source, extra))
        out.append((kind, text, payload_extra))
    return dedupe_entries_by_source_key(out)


__all__ = [
    "SOURCE_PROJECTION_KIND",
    "build_review_entries",
    "dedupe_entries_by_source_key",
    "entries_with_source_metadata",
    "expected_source_entries",
    "lookup_source_entry",
]
