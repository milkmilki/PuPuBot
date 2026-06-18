"""People indexes for event-thread memories."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..sessions import OWNER_SESSION
from .db import get_conn

OWNER_PERSON_KEY = "owner"
INSTANCE_PERSON_KEY = "instance"
DEFAULT_OWNER_DISPLAY = "用户"
DEFAULT_INSTANCE_DISPLAY = "实例"
VALID_EVENT_PERSON_ROLES = {"origin", "participant", "actor", "instance", "mentioned"}


def _now() -> str:
    return datetime.now().isoformat()


def _text(value: Any, fallback: str = "") -> str:
    return str(value if value is not None else fallback).strip()


def normalize_person_key(value: str | None) -> str:
    raw = _text(value).lower()
    if not raw:
        return ""
    if raw in {"user", "me"}:
        return OWNER_PERSON_KEY
    if raw in {"bot", "assistant", "pupu"}:
        return INSTANCE_PERSON_KEY
    normalized = re.sub(r"\s+", "-", raw)
    normalized = re.sub(r"[^0-9a-zA-Z:_\-\u4e00-\u9fff]+", "-", normalized)
    return normalized.strip("-")[:120]


def qq_person_key(user_id: object) -> str:
    raw = _text(user_id)
    return f"qq:{raw}" if raw else ""


def qqofficial_person_key(user_id: object) -> str:
    raw = _text(user_id)
    return f"qqofficial:{raw}" if raw else ""


def person_from_session(session_id: str) -> str:
    sid = _text(session_id)
    if sid == OWNER_SESSION:
        return OWNER_PERSON_KEY
    if sid.startswith("private_"):
        return qq_person_key(sid.removeprefix("private_"))
    if sid.startswith("c2c_"):
        return qqofficial_person_key(sid.removeprefix("c2c_"))
    return normalize_person_key(sid)


def default_owner_person() -> dict[str, Any]:
    return {
        "person_key": OWNER_PERSON_KEY,
        "kind": "owner",
        "display_name": DEFAULT_OWNER_DISPLAY,
    }


def default_instance_person(display_name: str | None = None) -> dict[str, Any]:
    return {
        "person_key": INSTANCE_PERSON_KEY,
        "kind": "instance",
        "display_name": _text(display_name) or DEFAULT_INSTANCE_DISPLAY,
    }


def _aliases_json(aliases: object) -> str:
    return json.dumps(sorted(set(_alias_values(aliases))), ensure_ascii=False)


def _alias_values(aliases: object) -> list[str]:
    if isinstance(aliases, str):
        text = aliases.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [_text(item) for item in parsed if _text(item)]
        values = [text]
    elif isinstance(aliases, (list, tuple, set)):
        values = [_text(item) for item in aliases if _text(item)]
    else:
        values = []
    return values


def _merge_aliases(*values: object) -> str:
    merged: list[str] = []
    for value in values:
        merged.extend(_alias_values(value))
    return _aliases_json(merged)


def _is_fixed_external_person(key: str) -> bool:
    return key.startswith("qq:") or key.startswith("qqofficial:")


def _canonical_kind_for_key(key: str, fallback: str = "") -> str:
    if key == OWNER_PERSON_KEY:
        return "owner"
    if key == INSTANCE_PERSON_KEY:
        return "instance"
    if key.startswith("qq:"):
        return "qq"
    if key.startswith("qqofficial:"):
        return "qqofficial"
    return _text(fallback)


def _is_default_display_for_key(key: str, display_name: str) -> bool:
    if key == OWNER_PERSON_KEY:
        return display_name == DEFAULT_OWNER_DISPLAY
    if key == INSTANCE_PERSON_KEY:
        return display_name == DEFAULT_INSTANCE_DISPLAY
    return False


def _insert_person_if_missing(
    conn,
    person: dict[str, Any],
    *,
    now: str | None = None,
) -> None:
    now = now or _now()
    key = normalize_person_key(person.get("person_key") if isinstance(person, dict) else "")
    if not key:
        return
    conn.execute(
        """INSERT OR IGNORE INTO people (
               person_key, kind, display_name, qq_id, aliases, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            key,
            _canonical_kind_for_key(key, person.get("kind", "")),
            _text(person.get("display_name")),
            _text(person.get("qq_id")),
            _aliases_json(person.get("aliases")),
            now,
            now,
        ),
    )


def upsert_person(
    conn,
    person_key: str,
    *,
    kind: str = "",
    display_name: str = "",
    qq_id: str = "",
    aliases: object = None,
    now: str | None = None,
) -> str:
    key = normalize_person_key(person_key)
    if not key:
        return ""
    now = now or _now()
    kind = _canonical_kind_for_key(key, kind)
    display_name = _text(display_name)
    qq_id = _text(qq_id)
    alias_values = _alias_values(aliases)
    existing = conn.execute(
        "SELECT kind, display_name, qq_id, aliases FROM people WHERE person_key = ?",
        (key,),
    ).fetchone()
    if existing:
        existing_kind = _text(existing["kind"])
        existing_display = _text(existing["display_name"])
        existing_qq_id = _text(existing["qq_id"])
        existing_aliases = existing["aliases"]
        next_kind = kind or existing_kind
        next_qq_id = qq_id or existing_qq_id
        preserve_existing_display = (
            (_is_fixed_external_person(key) and existing_display)
            or (
                key == OWNER_PERSON_KEY
                and existing_display
                and not _is_default_display_for_key(key, existing_display)
            )
        )
        if preserve_existing_display:
            next_display = existing_display
            if display_name and display_name != existing_display:
                alias_values.append(display_name)
        elif (
            existing_display
            and _is_default_display_for_key(key, existing_display)
            and display_name
            and not _is_default_display_for_key(key, display_name)
        ):
            next_display = display_name
            alias_values.append(existing_display)
        else:
            next_display = display_name or existing_display
            if existing_display and display_name and display_name != existing_display:
                alias_values.append(existing_display)
        aliases_json = _merge_aliases(existing_aliases, alias_values)
        conn.execute(
            """UPDATE people
               SET kind = ?, display_name = ?, qq_id = ?, aliases = ?, updated_at = ?
               WHERE person_key = ?""",
            (next_kind, next_display, next_qq_id, aliases_json, now, key),
        )
        return key

    aliases_json = _aliases_json(alias_values)
    conn.execute(
        """INSERT INTO people (
               person_key, kind, display_name, qq_id, aliases, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, kind, display_name, qq_id, aliases_json, now, now),
    )
    return key


def _add_range_person(people: dict[str, dict[str, Any]], person: dict[str, Any]) -> None:
    key = normalize_person_key(person.get("person_key") if isinstance(person, dict) else "")
    if not key:
        return
    incoming = {
        "person_key": key,
        "kind": _text(person.get("kind")) if isinstance(person, dict) else "",
        "display_name": _text(person.get("display_name")) if isinstance(person, dict) else "",
        "qq_id": _text(person.get("qq_id")) if isinstance(person, dict) else "",
        "aliases": _alias_values(person.get("aliases")) if isinstance(person, dict) else [],
    }
    existing = people.get(key)
    if not existing:
        people[key] = incoming
        return
    if not _text(existing.get("kind")) and incoming["kind"]:
        existing["kind"] = incoming["kind"]
    if not _text(existing.get("display_name")) and incoming["display_name"]:
        existing["display_name"] = incoming["display_name"]
    if not _text(existing.get("qq_id")) and incoming["qq_id"]:
        existing["qq_id"] = incoming["qq_id"]
    aliases = _alias_values(existing.get("aliases"))
    if incoming["display_name"] and incoming["display_name"] != _text(existing.get("display_name")):
        aliases.append(incoming["display_name"])
    aliases.extend(_alias_values(incoming.get("aliases")))
    existing["aliases"] = sorted(set(alias for alias in aliases if alias))


def _apply_known_people(conn, people: dict[str, dict[str, Any]]) -> None:
    for key, person in list(people.items()):
        row = conn.execute(
            "SELECT display_name, aliases FROM people WHERE person_key = ?",
            (key,),
        ).fetchone()
        if not row:
            continue
        known_display = _text(row["display_name"])
        current_display = _text(person.get("display_name"))
        aliases = _alias_values(row["aliases"])
        aliases.extend(_alias_values(person.get("aliases")))
        if known_display and not (
            _is_default_display_for_key(key, known_display)
            and current_display
            and not _is_default_display_for_key(key, current_display)
        ):
            if current_display and current_display != known_display:
                aliases.append(current_display)
            person["display_name"] = known_display
        elif known_display and current_display and current_display != known_display:
            aliases.append(known_display)
        person["aliases"] = sorted(set(alias for alias in aliases if alias))


def ensure_default_people(conn, *, instance_name: str | None = None, now: str | None = None) -> None:
    owner = default_owner_person()
    _insert_person_if_missing(conn, owner, now=now)
    instance = default_instance_person(instance_name)
    _insert_person_if_missing(conn, instance, now=now)


def person_from_message_sender(
    *,
    session_id: str,
    role: str,
    speaker_key: str = "",
    speaker_name: str = "",
    speaker_qq: str = "",
) -> dict[str, Any]:
    if role == "assistant":
        return default_instance_person(speaker_name)
    raw_speaker_key = _text(speaker_key)
    if raw_speaker_key.startswith("["):
        try:
            values = json.loads(raw_speaker_key)
        except Exception:
            values = []
        if isinstance(values, list) and values:
            first = values[0] if isinstance(values[0], dict) else {}
            key = normalize_person_key(first.get("person_key")) or OWNER_PERSON_KEY
            return {
                "person_key": key,
                "kind": _canonical_kind_for_key(key, _text(first.get("kind")) or "user"),
                "display_name": _text(first.get("display_name")) or _text(first.get("person_key")) or "用户",
                "qq_id": _text(first.get("qq_id")),
            }
    key = normalize_person_key(speaker_key)
    if not key:
        key = qq_person_key(speaker_qq) if speaker_qq else person_from_session(session_id)
    return {
        "person_key": key,
        "kind": _canonical_kind_for_key(key, "user"),
        "display_name": _text(speaker_name) or ("用户" if key == OWNER_PERSON_KEY else key),
        "qq_id": _text(speaker_qq),
    }


def resolve_person_for_prompt(
    *,
    person_key: str = "",
    qq_id: str = "",
    display_name: str = "",
    kind: str = "user",
) -> dict[str, Any]:
    """Resolve a prompt-facing person label, preferring stable DB identity.

    Runtime group nicknames are allowed to change. Prompt text should therefore
    resolve by ``person_key`` / QQ id first and use the stored display name when
    one exists.
    """
    key = normalize_person_key(person_key)
    qq = _text(qq_id)
    raw_display = _text(display_name)
    conn = get_conn()
    try:
        row = None
        if key:
            row = conn.execute(
                "SELECT person_key, kind, display_name, qq_id FROM people WHERE person_key = ?",
                (key,),
            ).fetchone()
        if row is None and qq:
            row = conn.execute(
                "SELECT person_key, kind, display_name, qq_id FROM people WHERE qq_id = ?",
                (qq,),
            ).fetchone()
        qq_key = qq_person_key(qq) if qq else ""
        if row is None and qq_key:
            row = conn.execute(
                "SELECT person_key, kind, display_name, qq_id FROM people WHERE person_key = ?",
                (qq_key,),
            ).fetchone()
    finally:
        conn.close()

    if row:
        resolved_key = _text(row["person_key"]) or key or qq_key
        resolved_kind = _text(row["kind"]) or _canonical_kind_for_key(resolved_key, kind)
        resolved_qq = _text(row["qq_id"]) or qq
        resolved_display = _text(row["display_name"]) or raw_display or resolved_key
        return {
            "person_key": resolved_key,
            "kind": resolved_kind,
            "display_name": resolved_display,
            "qq_id": resolved_qq,
        }

    resolved_key = key or qq_key
    return {
        "person_key": resolved_key,
        "kind": _canonical_kind_for_key(resolved_key, kind),
        "display_name": raw_display or resolved_key or DEFAULT_OWNER_DISPLAY,
        "qq_id": qq,
    }


def get_people_for_message_range(
    conn,
    session_id: str,
    start_msg_id: int | None,
    end_msg_id: int | None,
    *,
    include_instance: bool = True,
    instance_name: str | None = None,
) -> list[dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    if start_msg_id is not None and end_msg_id is not None:
        rows = conn.execute(
            """SELECT role, speaker_key, speaker_name, speaker_qq
               FROM messages
               WHERE session_id = ? AND id >= ? AND id <= ?
               ORDER BY id ASC""",
            (session_id, int(start_msg_id), int(end_msg_id)),
        ).fetchall()
        for row in rows:
            raw_speaker_key = str(row["speaker_key"] or "").strip()
            if str(row["role"] or "") == "user" and raw_speaker_key.startswith("["):
                try:
                    speakers = json.loads(raw_speaker_key)
                except Exception:
                    speakers = []
                if isinstance(speakers, list):
                    for speaker in speakers:
                        if not isinstance(speaker, dict):
                            continue
                        key = normalize_person_key(speaker.get("person_key"))
                        _add_range_person(
                            people,
                            {
                                "person_key": key,
                                "kind": _canonical_kind_for_key(
                                    key,
                                    _text(speaker.get("kind")) or "user",
                                ),
                                "display_name": _text(speaker.get("display_name"))
                                or _text(speaker.get("person_key")),
                                "qq_id": _text(speaker.get("qq_id")),
                                "aliases": speaker.get("aliases"),
                            },
                        )
                    continue
            person = person_from_message_sender(
                session_id=session_id,
                role=str(row["role"] or ""),
                speaker_key=str(row["speaker_key"] or ""),
                speaker_name=str(row["speaker_name"] or ""),
                speaker_qq=str(row["speaker_qq"] or ""),
            )
            _add_range_person(people, person)

    if not people:
        owner = default_owner_person()
        people[owner["person_key"]] = owner
    if include_instance:
        instance = default_instance_person(instance_name)
        people[instance["person_key"]] = instance
    _apply_known_people(conn, people)
    return list(people.values())


def list_people_for_message_range(
    session_id: str,
    start_msg_id: int,
    end_msg_id: int,
    *,
    include_instance: bool = True,
) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        return get_people_for_message_range(
            conn,
            session_id,
            start_msg_id,
            end_msg_id,
            include_instance=include_instance,
        )
    finally:
        conn.close()


def attach_event_people(
    conn,
    thread_id: int,
    people: list[dict[str, Any]],
    *,
    step_id: int | None = None,
    origin_person_key: str | None = None,
    source: str = "inferred",
    now: str | None = None,
) -> None:
    now = now or _now()
    ensure_default_people(conn, now=now)
    seen: set[tuple[str, str]] = set()
    origin_key = normalize_person_key(origin_person_key)
    normalized_people: dict[str, dict[str, Any]] = {}
    for person in people or []:
        if isinstance(person, dict):
            _add_range_person(normalized_people, person)
    _apply_known_people(conn, normalized_people)
    for person in normalized_people.values():
        key = normalize_person_key(person.get("person_key") if isinstance(person, dict) else "")
        if not key:
            continue
        kind = _text(person.get("kind")) if isinstance(person, dict) else ""
        display_name = _text(person.get("display_name")) if isinstance(person, dict) else ""
        qq_id = _text(person.get("qq_id")) if isinstance(person, dict) else ""
        aliases = person.get("aliases") if isinstance(person, dict) else None
        upsert_person(
            conn,
            key,
            kind=kind,
            display_name=display_name,
            qq_id=qq_id,
            aliases=aliases,
            now=now,
        )
        roles: list[str] = []
        if key == INSTANCE_PERSON_KEY:
            roles.append("instance")
        else:
            roles.append("participant")
            if origin_key and key == origin_key:
                roles.append("origin")
        for role in roles:
            if role not in VALID_EVENT_PERSON_ROLES:
                continue
            signature = (key, role)
            if signature in seen:
                continue
            seen.add(signature)
            if step_id is None:
                exists = conn.execute(
                    """SELECT 1 FROM event_people
                       WHERE thread_id = ? AND step_id IS NULL
                         AND person_key = ? AND role = ?
                       LIMIT 1""",
                    (int(thread_id), key, role),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO event_people (
                           thread_id, step_id, person_key, role, source, created_at
                       ) VALUES (?, NULL, ?, ?, ?, ?)""",
                    (int(thread_id), key, role, _text(source) or "inferred", now),
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO event_people (
                           thread_id, step_id, person_key, role, source, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (int(thread_id), int(step_id), key, role, _text(source) or "inferred", now),
                )


def get_event_people_for_thread_ids(conn, thread_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not thread_ids:
        return {}
    placeholders = ",".join("?" for _ in thread_ids)
    rows = conn.execute(
        f"""SELECT ep.thread_id, ep.step_id, ep.person_key, ep.role, ep.source,
                  p.kind, p.display_name, p.qq_id, p.aliases
           FROM event_people ep
           LEFT JOIN people p ON p.person_key = ep.person_key
           WHERE ep.thread_id IN ({placeholders})
           ORDER BY ep.thread_id ASC, ep.step_id IS NOT NULL ASC,
                    CASE ep.role
                        WHEN 'origin' THEN 0
                        WHEN 'participant' THEN 1
                        WHEN 'instance' THEN 2
                        ELSE 3
                    END,
                    ep.person_key ASC""",
        tuple(int(item) for item in thread_ids),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    seen: dict[int, set[tuple[str, str, int | None]]] = {}
    for row in rows:
        thread_id = int(row["thread_id"])
        step_id = row["step_id"]
        signature = (str(row["person_key"]), str(row["role"]), int(step_id) if step_id else None)
        if signature in seen.setdefault(thread_id, set()):
            continue
        seen[thread_id].add(signature)
        grouped.setdefault(thread_id, []).append(dict(row))
    return grouped


def get_thread_people(conn, thread_id: int) -> list[dict[str, Any]]:
    return get_event_people_for_thread_ids(conn, [int(thread_id)]).get(int(thread_id), [])


def format_people_label(people: list[dict[str, Any]] | None) -> str:
    if not people:
        return ""
    labels: list[str] = []
    seen: set[str] = set()
    for person in people:
        key = normalize_person_key(person.get("person_key") if isinstance(person, dict) else "")
        if not key or key in seen:
            continue
        seen.add(key)
        name = _text(person.get("display_name")) if isinstance(person, dict) else ""
        labels.append(name or key)
    return " / ".join(labels)


def backfill_default_event_people(conn, session_id: str | None = None) -> int:
    ensure_default_people(conn)
    params: list[Any] = []
    where = ""
    if session_id:
        where = " WHERE session_id = ?"
        params.append(session_id)
    threads = conn.execute(
        f"SELECT id, origin_person_key FROM event_threads{where}",
        tuple(params),
    ).fetchall()
    changed = 0
    for thread in threads:
        thread_id = int(thread["id"])
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM event_people WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()["c"]
        origin = normalize_person_key(thread["origin_person_key"]) or OWNER_PERSON_KEY
        if not normalize_person_key(thread["origin_person_key"]):
            conn.execute(
                "UPDATE event_threads SET origin_person_key = ? WHERE id = ?",
                (origin, thread_id),
            )
        if count:
            continue
        people = [default_owner_person(), default_instance_person()]
        attach_event_people(
            conn,
            thread_id,
            people,
            origin_person_key=origin,
            source="legacy_backfill",
        )
        step_rows = conn.execute(
            "SELECT id FROM event_steps WHERE thread_id = ? ORDER BY id ASC",
            (thread_id,),
        ).fetchall()
        for step in step_rows:
            attach_event_people(
                conn,
                thread_id,
                people,
                step_id=int(step["id"]),
                origin_person_key=origin,
                source="legacy_backfill",
            )
        changed += 1
    return changed
