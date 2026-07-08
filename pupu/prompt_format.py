"""Pure prompt / transcript / speaker formatting helpers extracted from agent.py."""

import json
import re

from .familiarity import (
    clamp_familiarity_score,
    default_familiarity_score,
    score_to_level,
)
from .memory import (
    find_related_event_threads,
    get_event_thread_recent_steps,
    list_scheduled_tasks,
)
from .message_sources import is_internal_message_source, message_source_label
from .storage.db import get_conn
from .storage.people import resolve_person_for_prompt


REVIEW_TASK_CONTEXT_LIMIT = 30
REVIEW_TASK_FIELD_LIMIT = 120


def _sanitize_review_speaker_name(value: object, fallback: str) -> str:
    text = str(value or "").replace("<end>", "").strip()
    text = re.sub(r"\s+", " ", text).strip("：:")
    if not text:
        return fallback
    lowered = text.lower()
    if lowered.startswith("qq:"):
        return fallback
    return text[:32]


def _known_review_people_map(people: list[dict]) -> dict[str, str]:
    known: dict[str, str] = {}
    for person in people or []:
        if not isinstance(person, dict):
            continue
        key = str(person.get("person_key") or "").strip()
        name = _sanitize_review_speaker_name(person.get("display_name"), "")
        if key and name:
            known[key] = name
        qq = str(person.get("qq_id") or "").strip()
        if qq and name:
            known[f"qq:{qq}"] = name
    return known


def _identity_session_for_group_person(person: dict | None) -> str:
    if not isinstance(person, dict):
        return ""
    key = str(person.get("person_key") or "").strip()
    kind = str(person.get("kind") or "").strip()
    qq_id = str(person.get("qq_id") or "").strip()
    if key == "owner" or kind == "owner":
        return "owner"
    if key.startswith("qq:"):
        qq_id = key.removeprefix("qq:")
    if qq_id:
        return f"private_{qq_id}"
    return ""


def _familiarity_level_for_identity_session(identity_session: str) -> str:
    sid = str(identity_session or "").strip()
    if not sid:
        return "认识"
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT score FROM familiarity WHERE session_id = ?",
            (sid,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return score_to_level(default_familiarity_score(sid))
    try:
        return score_to_level(clamp_familiarity_score(int(row["score"]), sid))
    except Exception:
        return "认识"


def _group_relationship_prefix_for_person(person: dict | None) -> str:
    if not isinstance(person, dict):
        return "认识"
    key = str(person.get("person_key") or "").strip()
    kind = str(person.get("kind") or "").strip()
    if key == "instance" or kind == "instance":
        return "自己"
    identity_session = _identity_session_for_group_person(person)
    if not identity_session:
        return "认识"
    return _familiarity_level_for_identity_session(identity_session)


def _group_relationship_prefix_for_label(
    label: str,
    people: list[dict],
) -> str:
    cleaned = str(label or "").strip()
    if not cleaned:
        return ""
    matches: list[str] = []
    for person in people or []:
        if not isinstance(person, dict):
            continue
        name = _sanitize_review_speaker_name(person.get("display_name"), "")
        key = str(person.get("person_key") or "").strip()
        if cleaned not in {name, key}:
            continue
        prefix = _group_relationship_prefix_for_person(person)
        if prefix and prefix not in matches:
            matches.append(prefix)
    if len(matches) == 1:
        return matches[0]
    return ""


def _format_group_relationship_speaker(
    label: str,
    people: list[dict],
) -> str:
    speaker = str(label or "").strip()
    if not speaker:
        return speaker
    prefix = _group_relationship_prefix_for_label(speaker, people)
    if not prefix:
        return speaker
    return f"“{prefix}”{speaker}"


def _format_group_people_context(
    people: list[dict],
    *,
    character_name: str,
) -> str:
    known_names = _known_review_people_map(people)
    lines = [f"你是{character_name}。"]
    seen: set[str] = set()
    for person in people or []:
        if not isinstance(person, dict):
            continue
        key = str(person.get("person_key") or "").strip()
        kind = str(person.get("kind") or "").strip()
        qq = str(person.get("qq_id") or "").strip()
        name = (
            known_names.get(key)
            or (known_names.get(f"qq:{qq}") if qq else "")
            or _sanitize_review_speaker_name(person.get("display_name"), "")
        )
        if not name or key in seen:
            continue
        seen.add(key)
        if key == "instance" or kind == "instance" or name == character_name:
            continue
        relation = _group_relationship_prefix_for_person(person)
        if relation:
            lines.append(f"{name}：与你的关系是{relation}。")
        else:
            lines.append(f"{name}：群聊成员。")
    return "\n".join(lines)


def _merge_people_for_prompt(*groups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for person in group or []:
            if not isinstance(person, dict):
                continue
            preferred_display = str(person.get("display_name") or "").strip()
            person = resolve_person_for_prompt(
                person_key=str(person.get("person_key") or ""),
                qq_id=str(person.get("qq_id") or ""),
                display_name=preferred_display,
                kind=str(person.get("kind") or "user"),
            )
            if preferred_display and preferred_display != str(person.get("display_name") or ""):
                aliases = person.setdefault("aliases", [])
                if not isinstance(aliases, list):
                    aliases = [str(aliases)]
                    person["aliases"] = aliases
                aliases.append(str(person.get("display_name") or ""))
                person["display_name"] = preferred_display
            key = str(person.get("person_key") or "").strip()
            qq = str(person.get("qq_id") or "").strip()
            merge_key = key or (f"qq:{qq}" if qq else "")
            if not merge_key:
                continue
            existing = merged.setdefault(merge_key, {})
            for field in ("person_key", "kind", "display_name", "qq_id", "aliases"):
                value = person.get(field)
                if value in (None, "", []):
                    continue
                if field == "display_name" and existing.get(field) and existing.get(field) != value:
                    aliases = existing.setdefault("aliases", [])
                    if not isinstance(aliases, list):
                        aliases = [str(aliases)]
                        existing["aliases"] = aliases
                    aliases.append(str(existing.get(field) or ""))
                    existing[field] = value
                    continue
                if field == "aliases" and existing.get(field):
                    aliases = existing.setdefault("aliases", [])
                    if isinstance(aliases, list):
                        aliases.extend(value if isinstance(value, list) else [str(value)])
                    continue
                if existing.get(field) in (None, "", []):
                    existing[field] = value
    return list(merged.values())


def _speaker_payload_from_message(item: dict) -> list[dict]:
    raw = str(item.get("speaker_key") or "").strip()
    if not raw.startswith("["):
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return [entry for entry in parsed if isinstance(entry, dict)] if isinstance(parsed, list) else []


def _review_name_for_person(
    person: dict,
    known_names: dict[str, str],
    *,
    fallback: str = "用户",
) -> str:
    key = str(person.get("person_key") or "").strip()
    if key and known_names.get(key):
        return known_names[key]
    return _sanitize_review_speaker_name(person.get("display_name"), fallback)


def _review_name_for_prefixed_qq(
    qq_id: str,
    raw_name: str,
    payload: list[dict],
    known_names: dict[str, str],
    prompt_people: list[dict] | None = None,
    include_relationship_prefix: bool = False,
) -> str:
    qq = str(qq_id or "").strip()
    if not qq:
        return ""
    for speaker in payload:
        if str(speaker.get("qq_id") or "").strip() != qq:
            continue
        key = str(speaker.get("person_key") or "").strip()
        if key and known_names.get(key):
            name = known_names[key]
            return (
                _format_group_relationship_speaker(name, prompt_people or [])
                if include_relationship_prefix
                else name
            )
        qq_key = f"qq:{qq}"
        if known_names.get(qq_key):
            name = known_names[qq_key]
            return (
                _format_group_relationship_speaker(name, prompt_people or [])
                if include_relationship_prefix
                else name
            )
        name = _sanitize_review_speaker_name(
            speaker.get("display_name") or raw_name,
            "用户",
        )
        return (
            _format_group_relationship_speaker(name, prompt_people or [])
            if include_relationship_prefix
            else name
        )
    qq_key = f"qq:{qq}"
    if known_names.get(qq_key):
        name = known_names[qq_key]
    else:
        name = _sanitize_review_speaker_name(raw_name, "用户")
    return (
        _format_group_relationship_speaker(name, prompt_people or [])
        if include_relationship_prefix
        else name
    )


def _review_speaker_name_for_message(
    item: dict,
    known_names: dict[str, str],
    character_name: str,
    prompt_people: list[dict] | None = None,
    include_relationship_prefix: bool = False,
) -> str:
    role = str(item.get("role") or "")
    if role == "assistant":
        row_name = _sanitize_review_speaker_name(item.get("speaker_name"), "")
        if row_name:
            return f"“自己”{row_name}" if include_relationship_prefix else row_name
        known_instance = known_names.get("instance") or ""
        if known_instance and known_instance != "实例":
            name = known_instance
        else:
            name = character_name
        return f"“自己”{name}" if include_relationship_prefix else name

    payload = _speaker_payload_from_message(item)
    if payload:
        names: list[str] = []
        for speaker in payload:
            name = _review_name_for_person(speaker, known_names)
            if name and name not in names:
                names.append(name)
        if names:
            label = " / ".join(names[:3])
            if len(names) > 3:
                label += " 等"
            return (
                _format_group_relationship_speaker(label, prompt_people or [])
                if include_relationship_prefix
                else label
            )

    key = str(item.get("speaker_key") or "").strip()
    if key and known_names.get(key):
        name = known_names[key]
        return (
            _format_group_relationship_speaker(name, prompt_people or [])
            if include_relationship_prefix
            else name
        )
    speaker_qq = str(item.get("speaker_qq") or "").strip()
    qq_key = f"qq:{speaker_qq}" if speaker_qq else ""
    if qq_key and known_names.get(qq_key):
        name = known_names[qq_key]
        return (
            _format_group_relationship_speaker(name, prompt_people or [])
            if include_relationship_prefix
            else name
        )
    if not key and known_names.get("owner"):
        name = known_names["owner"]
    else:
        name = _sanitize_review_speaker_name(item.get("speaker_name"), "用户")
    return (
        _format_group_relationship_speaker(name, prompt_people or [])
        if include_relationship_prefix
        else name
    )


def _format_prefixed_group_review_lines(
    content: str,
    payload: list[dict],
    known_names: dict[str, str],
    prompt_people: list[dict] | None = None,
    include_relationship_prefix: bool = False,
) -> list[str]:
    if "QQ:" not in content:
        return []

    lines: list[str] = []
    current_speaker = ""
    prefix_re = re.compile(r"^\s*\[(?:bot\s+)?(?P<name>.+?)\(QQ:(?P<qq>\d+)\)\]\s*(?P<text>.*)$")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = prefix_re.match(line)
        if match:
            speaker = _review_name_for_prefixed_qq(
                match.group("qq"),
                match.group("name"),
                payload,
                known_names,
                prompt_people=prompt_people,
                include_relationship_prefix=include_relationship_prefix,
            )
            if speaker:
                current_speaker = speaker
                text = match.group("text").replace("<end>", "[end]").strip()
                text = _strip_leading_speaker_prefix(text, [speaker])
                if text:
                    lines.append(f"{speaker}：{text} <end>")
                continue
        if current_speaker:
            text = _strip_leading_speaker_prefix(
                line.replace("<end>", "[end]"),
                [current_speaker],
            )
            if text:
                lines.append(f"{current_speaker}：{text} <end>")

    return lines


def _strip_leading_speaker_prefix(text: object, speaker_names: list[str] | tuple[str, ...]) -> str:
    cleaned = str(text or "").replace("<end>", "[end]").strip()
    names: list[str] = []
    for raw_name in speaker_names:
        name = str(raw_name or "").strip().strip(":：")
        if name and name not in names:
            names.append(name)
        plain_name = re.sub(r"^“[^”]+”", "", name).strip()
        if plain_name and plain_name not in names:
            names.append(plain_name)
    if not cleaned or not names:
        return cleaned

    for _ in range(4):
        before = cleaned
        for name in names:
            cleaned = re.sub(
                r"^\s*(?:“[^”]+”\s*)?" + re.escape(name) + r"\s*[:：]\s*",
                "",
                cleaned,
                count=1,
            )
        cleaned = cleaned.lstrip()
        if cleaned == before:
            break
    return cleaned


def _split_leading_turn_timestamp(text: object) -> tuple[str, str]:
    cleaned = str(text or "").strip()
    match = re.match(r"^\s*(?P<stamp>\[时间:[^\]]+\])\s*(?P<body>.*)$", cleaned, flags=re.DOTALL)
    if not match:
        return "", cleaned
    return match.group("stamp").strip(), match.group("body").strip()


def _format_review_conversation_transcript(
    batch: list[dict],
    *,
    character_name: str,
    people: list[dict],
) -> str:
    known_names = _known_review_people_map(people)
    lines: list[str] = []
    for item in batch:
        content = str(item.get("content") or "").replace("<end>", "[end]").strip()
        if not content:
            continue
        turn_timestamp, content = _split_leading_turn_timestamp(content)
        payload = _speaker_payload_from_message(item)
        prefixed_lines = _format_prefixed_group_review_lines(content, payload, known_names)
        if prefixed_lines:
            if turn_timestamp:
                lines.extend(f"{turn_timestamp} {line}" for line in prefixed_lines)
            else:
                lines.extend(prefixed_lines)
            continue
        speaker = _review_speaker_name_for_message(item, known_names, character_name)
        content = _strip_leading_speaker_prefix(content, [speaker])
        prefix = f"{turn_timestamp} " if turn_timestamp else ""
        lines.append(f"{prefix}{speaker}：{content} <end>")
    return "\n".join(lines)


def _format_message_content_for_prompt(
    item: dict,
    *,
    character_name: str,
    people: list[dict],
    include_end_marker: bool = False,
    include_relationship_prefix: bool = False,
    bare_assistant: bool = False,
) -> str:
    known_names = _known_review_people_map(people)
    content = str(item.get("content") or "").replace("<end>", "[end]").strip()
    if not content:
        return ""
    turn_timestamp, content = _split_leading_turn_timestamp(content)
    source = item.get("source")
    if is_internal_message_source(source):
        speaker = message_source_label(item.get("role"), source, character_name)
        prefix = f"{turn_timestamp} " if turn_timestamp else ""
        return f"{prefix}{speaker}：{content}"
    if bare_assistant and str(item.get("role") or "") == "assistant":
        speaker = _review_speaker_name_for_message(item, known_names, character_name)
        return _strip_leading_speaker_prefix(content, [speaker])
    payload = _speaker_payload_from_message(item)
    prompt_people = _merge_people_for_prompt(payload, people)
    prompt_names = _known_review_people_map(prompt_people)
    prompt_names.update(known_names)
    prefixed_lines = _format_prefixed_group_review_lines(
        content,
        payload,
        prompt_names,
        prompt_people=prompt_people,
        include_relationship_prefix=include_relationship_prefix,
    )
    if prefixed_lines:
        if turn_timestamp:
            prefixed_lines = [f"{turn_timestamp} {line}" for line in prefixed_lines]
        if include_end_marker:
            return "\n".join(prefixed_lines)
        return "\n".join(line.removesuffix(" <end>") for line in prefixed_lines)
    speaker = _review_speaker_name_for_message(
        item,
        known_names,
        character_name,
        prompt_people=people,
        include_relationship_prefix=include_relationship_prefix,
    )
    content = _strip_leading_speaker_prefix(content, [speaker])
    suffix = " <end>" if include_end_marker else ""
    prefix = f"{turn_timestamp} " if turn_timestamp else ""
    return f"{prefix}{speaker}：{content}{suffix}"


def _format_chat_history_for_prompt(
    history: list[dict],
    *,
    character_name: str,
    people: list[dict],
    include_relationship_prefix: bool = False,
    bare_assistant: bool = False,
) -> list[dict]:
    formatted: list[dict] = []
    for item in history or []:
        role = str(item.get("role") or "")
        content = _format_message_content_for_prompt(
            item,
            character_name=character_name,
            people=people,
            include_relationship_prefix=include_relationship_prefix,
            bare_assistant=bare_assistant,
        )
        if content:
            formatted.append({"role": role, "content": content})
    return formatted


def _compact_review_field(value: object, limit: int = REVIEW_TASK_FIELD_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _format_active_scheduled_tasks_for_review(
    session_id: str,
    limit: int = REVIEW_TASK_CONTEXT_LIMIT,
) -> str:
    rows = list_scheduled_tasks(session_id)
    if not rows:
        return "当前已有定时任务：无"

    visible_rows = rows[:limit]
    lines = [
        "当前已有定时任务（只用于判断 task_updates；输出时不要使用 id，"
        "请用能匹配标题或内容的 query）："
    ]
    for index, row in enumerate(visible_rows, start=1):
        repeat = str(row.get("repeat_kind") or "once")
        interval_seconds = row.get("interval_seconds")
        if repeat == "interval" and interval_seconds:
            repeat = f"interval/{interval_seconds}s"
        lines.append(
            "- "
            f"#{index} id={row.get('id')} | "
            f"title={_compact_review_field(row.get('title'), 48)} | "
            f"run_at={_compact_review_field(row.get('run_at'), 32)} | "
            f"repeat={repeat} | "
            f"instruction={_compact_review_field(row.get('instruction'))}"
        )
    if len(rows) > limit:
        lines.append(f"- 还有 {len(rows) - limit} 条未列出")
    return "\n".join(lines)


def _format_event_thread_candidates_for_review(
    identity_session: str,
    text: str,
    *,
    person_keys: set[str] | None = None,
) -> str:
    candidates = find_related_event_threads(
        identity_session,
        text,
        limit=5,
        person_keys=person_keys,
    )
    if not candidates:
        return "候选事件线：无"
    lines = ["候选事件线（优先把新进展归并到这些 thread_key；确实无关才 create_thread）："]
    for item in candidates:
        key = str(item.get("thread_key") or "")
        title = str(item.get("title") or "未命名事件")
        status = str(item.get("status") or "active")
        current = str(item.get("current_summary") or item.get("details") or "")
        hint = str(item.get("followup_hint") or item.get("merge_hint") or "")
        people_label = str(item.get("people_label") or "")
        score = float(item.get("score") or 0.0)
        line = f"- thread_key={key} | score={score:.2f} | status={status} | title={title}"
        if people_label:
            line += f" | people={_compact_review_field(people_label, 80)}"
        if current:
            line += f" | current={_compact_review_field(current, 120)}"
        if hint:
            line += f" | hint={_compact_review_field(hint, 100)}"
        lines.append(line)
        steps = get_event_thread_recent_steps(identity_session, key, limit=3) if key else []
        for step in steps:
            step_type = str(step.get("step_type") or "user")
            summary = _compact_review_field(step.get("summary"), 90)
            cause = _compact_review_field(step.get("cause"), 70)
            reflection = _compact_review_field(step.get("reflection"), 70)
            step_line = f"  - recent_step[{step_type}] summary={summary}"
            if cause:
                step_line += f" | cause={cause}"
            if reflection:
                step_line += f" | reflection={reflection}"
            lines.append(step_line)
    return "\n".join(lines)
