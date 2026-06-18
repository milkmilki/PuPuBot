"""Core chat agent: builds prompts, calls model APIs, and persists conversation memory."""

import json
import re
import threading
import traceback
from datetime import datetime

from .llm import (
    JUDGE_MODEL,
    MODEL,
    chat_complete,
    json_task,
    last_provider_label,
    provider_label,
)
from .memory import (
    count_pending_review_turns,
    get_familiarity,
    find_related_event_threads,
    has_successful_memu_sync,
    get_event_thread_recent_steps,
    get_event_threads,
    get_person_facts,
    get_oldest_unsummarized_msg_id,
    get_recent_messages,
    get_review_candidate_batch,
    get_summaries,
    list_scheduled_tasks,
    list_pending_review_sessions,
    list_people_for_message_range,
    person_from_session,
    record_memu_sync,
    save_message,
    save_message_with_speaker,
    save_summary,
    update_familiarity,
    upsert_person_facts,
)
from .memory_index import is_memu_long_term_enabled, recall_memories, sync_review_memory
from .followup import DIALOGUE_OUTPUT_PROTOCOL, _parse_dialogue_output
from .message_sources import CHAT
from .persona import build_batch_review_prompt, build_system_prompt, get_pupu_name
from .review_followups import (
    apply_review_task_updates,
    normalize_review_event_updates,
    normalize_review_task_updates,
    save_review_event_updates,
)
from .storage.people import resolve_person_for_prompt
from .tools import execute_tool, get_chat_tool_definitions, is_admin_tool

REVIEW_INTERVAL = 10
REVIEW_SOURCE = CHAT
CHAT_HISTORY_LIMIT = 10
PROMPT_SUMMARY_LIMIT = 2
PROMPT_EVENT_THREAD_LIMIT = 6
BATCH_REVIEW_MAX_TOKENS = 10000
REVIEW_TASK_CONTEXT_LIMIT = 30
REVIEW_TASK_FIELD_LIMIT = 120
_batch_review_lock = threading.Lock()


def _clean_fact_scalar(value) -> str:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _normalize_person_facts(value) -> list[dict]:
    if isinstance(value, dict):
        value = [
            {"subject": "owner", "scope": "person", "key": key, "value": item}
            for key, item in value.items()
        ]
    if not isinstance(value, list):
        return []
    cleaned: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = _clean_fact_scalar(item.get("key") or item.get("fact_key"))
        val = _clean_fact_scalar(item.get("value") or item.get("fact_value"))
        subject = _clean_fact_scalar(item.get("subject") or item.get("subject_person_key"))
        obj = _clean_fact_scalar(item.get("object") or item.get("object_person_key"))
        scope = (_clean_fact_scalar(item.get("scope") or "person").lower() or "person")
        if not key or not val:
            continue
        cleaned.append(
            {
                "subject": subject,
                "object": obj,
                "scope": scope,
                "key": key,
                "value": val,
                "confidence": item.get("confidence", 1.0),
            }
        )
    return cleaned


def _normalize_familiarity_delta(value) -> int:
    try:
        delta = int(value or 0)
    except Exception:
        return 0
    return max(-20, min(20, delta))


def _normalize_batch_review_result(value) -> dict:
    if not isinstance(value, dict):
        return {
            "summary": "",
            "familiarity_delta": 0,
            "person_facts": [],
            "event_updates": [],
            "task_updates": [],
        }

    event_updates = normalize_review_event_updates(value.get("event_updates", []))

    return {
        "summary": str(value.get("summary", "")).strip(),
        "familiarity_delta": _normalize_familiarity_delta(
            value.get("familiarity_delta", 0)
        ),
        "person_facts": _normalize_person_facts(value.get("person_facts", [])),
        "event_updates": event_updates,
        "task_updates": normalize_review_task_updates(value.get("task_updates", [])),
    }


def _parse_batch_review_result(raw_text: str) -> dict:
    cleaned = _strip_code_fence(raw_text)
    decoder = json.JSONDecoder()
    candidates = []
    if cleaned:
        candidates.append(cleaned)
        brace_index = cleaned.find("{")
        if brace_index != -1:
            candidates.append(cleaned[brace_index:])

    seen = set()
    for candidate in candidates:
        variants = [
            candidate,
            re.sub(r",\s*([}\]])", r"\1", candidate),
            _repair_unescaped_quotes_in_json_strings(candidate),
            _repair_unescaped_quotes_in_json_strings(
                re.sub(r",\s*([}\]])", r"\1", candidate)
            ),
        ]
        for variant in variants:
            if variant in seen:
                continue
            seen.add(variant)
            try:
                parsed, _ = decoder.raw_decode(variant)
            except Exception:
                continue
            return _normalize_batch_review_result(parsed)

    raise ValueError("unable to parse batch review response as JSON object")


def _build_fallback_summary(
    batch: list[dict],
    *,
    character_name: str | None = None,
) -> str:
    assistant_name = str(character_name or "").strip() or get_pupu_name()
    turn_snippets = []
    current_turn = []
    for message in batch:
        speaker = "用户" if message["role"] == "user" else assistant_name
        text = " ".join(str(message["content"]).split())
        current_turn.append(f"{speaker}:{text[:80]}")
        if message["role"] == "assistant":
            turn_snippets.append(" / ".join(current_turn))
            current_turn = []

    if current_turn:
        turn_snippets.append(" / ".join(current_turn))

    if not turn_snippets:
        return "这轮对话内容较少，没有足够信息形成具体摘要。"

    preview = " ; ".join(turn_snippets[:4])
    if len(turn_snippets) > 4:
        preview += f" ; 另有 {len(turn_snippets) - 4} 轮对话"
    return f"对话批次摘要：{preview}"[:220]


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
        limit=6,
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


def _sanitize_review_speaker_name(value: object, fallback: str) -> str:
    text = str(value or "").replace("<end>", "").strip()
    text = re.sub(r"\s+", " ", text).strip("：:")
    if not text:
        return fallback
    lowered = text.lower()
    if lowered.startswith("qq:") or lowered.startswith("qqofficial:"):
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


def _merge_people_for_prompt(*groups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for person in group or []:
            if not isinstance(person, dict):
                continue
            key = str(person.get("person_key") or "").strip()
            qq = str(person.get("qq_id") or "").strip()
            merge_key = key or (f"qq:{qq}" if qq else "")
            if not merge_key:
                continue
            existing = merged.setdefault(merge_key, {})
            for field in ("person_key", "kind", "display_name", "qq_id", "aliases"):
                value = person.get(field)
                if value not in (None, "", []):
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
) -> str:
    qq = str(qq_id or "").strip()
    if not qq:
        return ""
    for speaker in payload:
        if str(speaker.get("qq_id") or "").strip() != qq:
            continue
        key = str(speaker.get("person_key") or "").strip()
        if key and known_names.get(key):
            return known_names[key]
        qq_key = f"qq:{qq}"
        if known_names.get(qq_key):
            return known_names[qq_key]
        return _sanitize_review_speaker_name(
            speaker.get("display_name") or raw_name,
            "用户",
        )
    qq_key = f"qq:{qq}"
    if known_names.get(qq_key):
        return known_names[qq_key]
    return _sanitize_review_speaker_name(raw_name, "用户")


def _review_speaker_name_for_message(
    item: dict,
    known_names: dict[str, str],
    character_name: str,
) -> str:
    role = str(item.get("role") or "")
    if role == "assistant":
        row_name = _sanitize_review_speaker_name(item.get("speaker_name"), "")
        if row_name:
            return row_name
        known_instance = known_names.get("instance") or ""
        if known_instance and known_instance != "实例":
            return known_instance
        return character_name

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
            return label

    key = str(item.get("speaker_key") or "").strip()
    if key and known_names.get(key):
        return known_names[key]
    if not key and known_names.get("owner"):
        return known_names["owner"]
    return _sanitize_review_speaker_name(item.get("speaker_name"), "用户")


def _format_prefixed_group_review_lines(
    content: str,
    payload: list[dict],
    known_names: dict[str, str],
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
            )
            if speaker:
                current_speaker = speaker
                text = match.group("text").replace("<end>", "[end]").strip()
                if text:
                    lines.append(f"{speaker}：{text} <end>")
                continue
        if current_speaker:
            lines.append(f"{current_speaker}：{line.replace('<end>', '[end]')} <end>")

    return lines


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
        payload = _speaker_payload_from_message(item)
        prefixed_lines = _format_prefixed_group_review_lines(content, payload, known_names)
        if prefixed_lines:
            lines.extend(prefixed_lines)
            continue
        speaker = _review_speaker_name_for_message(item, known_names, character_name)
        lines.append(f"{speaker}：{content} <end>")
    return "\n".join(lines)


def _format_message_content_for_prompt(
    item: dict,
    *,
    character_name: str,
    people: list[dict],
    include_end_marker: bool = False,
) -> str:
    known_names = _known_review_people_map(people)
    content = str(item.get("content") or "").replace("<end>", "[end]").strip()
    if not content:
        return ""
    payload = _speaker_payload_from_message(item)
    prompt_people = _merge_people_for_prompt(payload, people)
    prompt_names = _known_review_people_map(prompt_people)
    prefixed_lines = _format_prefixed_group_review_lines(content, payload, prompt_names)
    if prefixed_lines:
        if include_end_marker:
            return "\n".join(prefixed_lines)
        return "\n".join(line.removesuffix(" <end>") for line in prefixed_lines)
    speaker = _review_speaker_name_for_message(item, known_names, character_name)
    suffix = " <end>" if include_end_marker else ""
    return f"{speaker}：{content}{suffix}"


def _format_chat_history_for_prompt(
    history: list[dict],
    *,
    character_name: str,
    people: list[dict],
) -> list[dict]:
    formatted: list[dict] = []
    for item in history or []:
        role = str(item.get("role") or "")
        content = _format_message_content_for_prompt(
            item,
            character_name=character_name,
            people=people,
        )
        if content:
            formatted.append({"role": role, "content": content})
    return formatted


def _format_turn_timestamp() -> str:
    """Return a compact local timestamp with weekday for each user turn."""
    now = datetime.now()
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return f"{now.strftime('%Y-%m-%d')} {weekdays[now.weekday()]} {now.strftime('%H:%M')}"


def _strip_code_fence(raw_text: str) -> str:
    raw = (raw_text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines:
        lines = lines[1:]
    raw = "\n".join(lines)
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _repair_unescaped_quotes_in_json_strings(raw_text: str) -> str:
    if not raw_text:
        return raw_text

    chars: list[str] = []
    in_string = False
    escaped = False
    length = len(raw_text)

    def _next_non_ws(index: int) -> str:
        j = index + 1
        while j < length and raw_text[j].isspace():
            j += 1
        return raw_text[j] if j < length else ""

    for i, ch in enumerate(raw_text):
        if not in_string:
            chars.append(ch)
            if ch == '"':
                in_string = True
            continue

        if escaped:
            chars.append(ch)
            escaped = False
            continue

        if ch == "\\":
            chars.append(ch)
            escaped = True
            continue

        if ch == '"':
            next_ch = _next_non_ws(i)
            if next_ch and next_ch not in {",", "}", "]", ":"}:
                chars.append('\\"')
                continue
            chars.append(ch)
            in_string = False
            continue

        chars.append(ch)

    return "".join(chars)


def chat(
    user_input: str,
    session_id: str = "default",
    is_admin: bool = False,
    image_urls: list[str] = None,
    reply_speed_hint: str = None,
    message_source: str = REVIEW_SOURCE,
    *,
    context_session: str | None = None,
    identity_session: str | None = None,
    persist_user: bool = True,
    speaker_key: str = "",
    speaker_name: str = "",
    speaker_qq: str = "",
) -> str:
    """Process one turn of conversation. Returns the assistant's text reply."""
    from .dialogue_loop import cancel_wait_timer, schedule_wait_timer

    context_session = str(context_session or session_id or "default")
    identity_session = str(identity_session or session_id or "default")

    cancel_wait_timer(context_session)

    display_text = user_input
    if image_urls:
        n = len(image_urls)
        hint = f"[用户发了{n}张图片]" if n > 1 else "[用户发了一张图片]"
        display_text = f"{hint} {user_input}" if user_input else hint

    display_text = f"[时间: {_format_turn_timestamp()}] {display_text}"

    if persist_user:
        save_message_with_speaker(
            "user",
            display_text,
            context_session,
            source=message_source,
            speaker_key=speaker_key,
            speaker_name=speaker_name,
            speaker_qq=speaker_qq,
        )

    history = get_recent_messages(CHAT_HISTORY_LIMIT, context_session)
    history_people: list[dict] = []
    if history:
        try:
            history_people = list_people_for_message_range(
                context_session,
                int(history[0].get("id") or 0),
                int(history[-1].get("id") or 0),
            )
        except Exception:
            history_people = []
    current_people = _merge_people_for_prompt(
        _speaker_payload_from_message(
            {
                "speaker_key": speaker_key,
                "speaker_name": speaker_name,
                "speaker_qq": speaker_qq,
            }
        ),
        [
            resolve_person_for_prompt(
                person_key=speaker_key,
                qq_id=speaker_qq,
                display_name=speaker_name,
                kind="user",
            )
        ]
        if (speaker_key or speaker_qq or speaker_name)
        else [],
        history_people,
    )
    prompt_display_text = _format_message_content_for_prompt(
        {
            "role": "user",
            "content": display_text,
            "speaker_key": speaker_key,
            "speaker_name": speaker_name,
            "speaker_qq": speaker_qq,
        },
        character_name=get_pupu_name(),
        people=current_people,
    )
    prompt_display_text = prompt_display_text or display_text

    score = get_familiarity(identity_session)
    recalled_memories = []
    if is_memu_long_term_enabled():
        recalled_memories = recall_memories(
            query=prompt_display_text,
            context_session=context_session,
            identity_session=identity_session,
            history=_format_chat_history_for_prompt(
                history,
                character_name=get_pupu_name(),
                people=history_people,
            ),
        )
        person_facts = []
        summaries = get_summaries(context_session, limit=PROMPT_SUMMARY_LIMIT)
        event_threads = []
    else:
        person_facts = get_person_facts(
            subject_person_keys=["instance", person_from_session(identity_session)],
            include_relationships=True,
        )
        summaries = get_summaries(context_session, limit=PROMPT_SUMMARY_LIMIT)
        event_threads = get_event_threads(identity_session, limit=PROMPT_EVENT_THREAD_LIMIT)
    system_prompt = build_system_prompt(
        score,
        summaries=summaries,
        person_facts=person_facts,
        event_threads=event_threads,
        reply_speed_hint=reply_speed_hint,
        recalled_memories=recalled_memories,
    )

    messages = _format_chat_history_for_prompt(
        history,
        character_name=get_pupu_name(),
        people=history_people,
    )

    def _tool_handler(tool_name: str, tool_input: dict, reason_hint: str | None = None):
        if is_admin_tool(tool_name) and not is_admin:
            return "权限不足：只有管理员才能使用文件和命令工具。"
        return execute_tool(
            tool_name,
            tool_input,
            image_urls=image_urls,
            session_id=context_session,
            reason_hint=reason_hint or None,
        )

    final_text_raw = chat_complete(
        role="chat",
        model=MODEL,
        system=system_prompt + DIALOGUE_OUTPUT_PROTOCOL,
        messages=messages,
        max_tokens=10000,
        tools=get_chat_tool_definitions(),
        tool_handler=_tool_handler,
        session_id=context_session,
        image_urls=image_urls,
        is_admin=is_admin,
        tool_exposure="chat",
    )
    final_text, should_wait = _parse_dialogue_output(final_text_raw)
    print(
        "[pupu] dialogue decision: "
        f"context={context_session} identity={identity_session} "
        f"source={message_source} should_wait={should_wait}"
    )

    save_message_with_speaker(
        "assistant",
        final_text,
        context_session,
        source=message_source,
        speaker_key="instance",
        speaker_name=get_pupu_name(),
    )

    if should_wait:
        schedule_wait_timer(context_session)

    if message_source == REVIEW_SOURCE:
        _maybe_batch_review(context_session, identity_session=identity_session)

    return final_text


def _maybe_batch_review(
    session_id: str = "default",
    *,
    context_session: str | None = None,
    identity_session: str | None = None,
):
    context_session = str(context_session or session_id or "default")
    identity_session = str(identity_session or session_id or "default")
    if not _batch_review_lock.acquire(blocking=False):
        print(
            "[pupu] batch review skip: lock busy "
            f"context={context_session} identity={identity_session}"
        )
        return
    try:
        return _maybe_batch_review_unlocked(context_session, identity_session=identity_session)
    finally:
        _batch_review_lock.release()


def run_due_batch_reviews():
    for session_id in list_pending_review_sessions(REVIEW_SOURCE):
        _maybe_batch_review(session_id)


def _maybe_batch_review_unlocked(
    session_id: str = "default",
    *,
    context_session: str | None = None,
    identity_session: str | None = None,
):
    """Every REVIEW_INTERVAL chat messages, summarize + judge familiarity + extract facts."""
    context_session = str(context_session or session_id or "default")
    identity_session = str(identity_session or session_id or "default")
    try:
        print(
            "[pupu] batch review check: "
            f"context={context_session}, identity={identity_session}, "
            f"interval={REVIEW_INTERVAL}"
        )
        last_reviewed = get_oldest_unsummarized_msg_id(context_session)
        pending_messages = count_pending_review_turns(
            session_id=context_session,
            after_msg_id=last_reviewed,
            source=REVIEW_SOURCE,
        )
        print(
            "[pupu] batch review context: "
            f"last_reviewed_id={last_reviewed}, pending_messages={pending_messages}"
        )

        trigger = ""
        review_messages = 0
        if pending_messages >= REVIEW_INTERVAL:
            trigger = "messages"
            review_messages = REVIEW_INTERVAL

        if not trigger:
            print(
                "[pupu] batch review skip: "
                f"need={REVIEW_INTERVAL}, got={pending_messages}"
            )
            return

        batch = get_review_candidate_batch(
            session_id=context_session,
            review_interval=review_messages,
            source=REVIEW_SOURCE,
            min_turns=review_messages,
        )
        if not batch:
            print("[pupu] batch review skip: candidate batch unavailable")
            return

        batch_turns = len(batch)
        print(
            "[pupu] batch review trigger: "
            f"trigger={trigger}, messages_count={batch_turns}, pending_messages={pending_messages}, "
            f"messages={len(batch)}, "
            f"msg_id_range={batch[0]['id']}..{batch[-1]['id']}"
        )

        active_tasks_text = _format_active_scheduled_tasks_for_review(context_session)
        character_name = get_pupu_name()
        batch_people = list_people_for_message_range(
            context_session,
            batch[0]["id"],
            batch[-1]["id"],
        )
        batch_person_keys = {
            str(person.get("person_key") or "")
            for person in batch_people
            if str(person.get("person_key") or "").strip()
        }
        conversation_text = _format_review_conversation_transcript(
            batch,
            character_name=character_name,
            people=batch_people,
        )
        event_candidates_text = _format_event_thread_candidates_for_review(
            identity_session,
            conversation_text,
            person_keys=batch_person_keys,
        )
        conversation_text = (
            f"Current local time: {datetime.now().isoformat(timespec='seconds')}\n\n"
            + active_tasks_text
            + "\n\n"
            + event_candidates_text
            + "\n\n待整理对话：\n"
            + conversation_text
        )
        familiarity_score = get_familiarity(identity_session)
        include_familiarity_delta = familiarity_score < 100
        review_prompt = build_batch_review_prompt(
            include_familiarity_delta=include_familiarity_delta,
            character_name=character_name,
        )
        print(
            "[pupu] batch review request: "
            f"provider={provider_label('judge', JUDGE_MODEL)}, "
            f"input_chars={len(conversation_text)}, "
            f"familiarity_score={familiarity_score}, "
            f"delta_enabled={include_familiarity_delta}"
        )

        raw_text = json_task(
            role="judge",
            model=JUDGE_MODEL,
            system=review_prompt,
            user_content=conversation_text,
            max_tokens=BATCH_REVIEW_MAX_TOKENS,
            task_name="batch_review",
        )
        print(
            "[pupu] batch review response: "
            f"provider={last_provider_label('judge', JUDGE_MODEL)}, chars={len(raw_text)}"
        )

        if not isinstance(raw_text, str):
            print(f"[pupu] batch review: unexpected response type {type(raw_text)}")
            raw_text = ""
        raw = raw_text.strip()
        preview = raw.replace("\n", " ")[:300]
        print(f"[pupu] batch review raw_preview(300)={preview}")

        try:
            result = _parse_batch_review_result(raw)
        except Exception as exc:
            print(f"[pupu] batch review json parse failed: {exc}")
            print(f"[pupu] batch review raw_full={raw}")
            result = {
                "summary": _build_fallback_summary(batch, character_name=character_name),
                "familiarity_delta": 0,
                "person_facts": [],
                "event_updates": [],
                "task_updates": [],
            }
            print("[pupu] batch review fallback summary enabled")

        summary = result.get("summary") or _build_fallback_summary(
            batch,
            character_name=character_name,
        )
        save_summary(summary, batch[0]["id"], batch[-1]["id"], context_session)
        print(
            "[pupu] batch review summary saved: "
            f"chars={len(summary)}, range={batch[0]['id']}..{batch[-1]['id']}"
        )

        familiarity_delta = int(result.get("familiarity_delta", 0) or 0)
        if not include_familiarity_delta and familiarity_delta:
            print(
                "[pupu] batch review familiarity delta ignored: "
                f"score already 100, model_delta={familiarity_delta}"
            )
            familiarity_delta = 0
        if familiarity_delta:
            update_familiarity(familiarity_delta, session_id=identity_session)
            print(
                "[pupu] batch review familiarity delta applied: "
                f"delta={familiarity_delta}"
            )
        else:
            print("[pupu] batch review familiarity delta empty")

        person_facts = list(result.get("person_facts", []) or [])
        saved_person_facts = []
        if person_facts:
            fact_known_people = list(batch_people or [])
            if character_name:
                fact_known_people.append(
                    {
                        "person_key": "instance",
                        "kind": "instance",
                        "display_name": character_name,
                    }
                )
            saved_person_facts = upsert_person_facts(
                person_facts,
                default_subject_person_key=person_from_session(identity_session),
                legacy_session_id=identity_session,
                known_people=fact_known_people,
                context_session=context_session,
                source_msg_start_id=batch[0]["id"],
                source_msg_end_id=batch[-1]["id"],
            )
            print(
                "[pupu] batch review person_facts upserted: "
                f"count={len(saved_person_facts)}"
            )
            saved_subject_keys = {
                str(item.get("subject_person_key") or "")
                for item in saved_person_facts
                if str(item.get("subject_person_key") or "").strip()
            }
            if saved_subject_keys:
                saved_signatures = {
                    (
                        str(item.get("subject_person_key") or ""),
                        str(item.get("object_person_key") or ""),
                        str(item.get("scope") or ""),
                        str(item.get("fact_key") or ""),
                    )
                    for item in saved_person_facts
                }
                fetched_person_facts = get_person_facts(
                    subject_person_keys=saved_subject_keys,
                    include_relationships=True,
                )
                saved_person_facts = [
                    item
                    for item in fetched_person_facts
                    if (
                        str(item.get("subject_person_key") or ""),
                        str(item.get("object_person_key") or ""),
                        str(item.get("scope") or ""),
                        str(item.get("fact_key") or ""),
                    )
                    in saved_signatures
                ]
        else:
            print("[pupu] batch review person_facts empty")

        event_updates = result.get("event_updates", [])
        saved_event_rows = {}
        if event_updates:
            saved_event_rows = save_review_event_updates(
                identity_session,
                event_updates,
                context_session=context_session,
                source_msg_start_id=batch[0]["id"],
                source_msg_end_id=batch[-1]["id"],
            )
            print(
                "[pupu] batch review event_updates saved: "
                f"count={len(saved_event_rows)}, keys={list(saved_event_rows.keys())[:6]}"
            )
        else:
            print("[pupu] batch review event_updates empty")
        event_threads = list(saved_event_rows.values())

        task_updates = result.get("task_updates", [])
        if task_updates:
            update_results = apply_review_task_updates(
                context_session,
                task_updates,
                saved_event_rows,
                identity_session=identity_session,
            )
            cancelled_count = sum(
                len(item.get("task_ids", []))
                for item in update_results
                if item.get("status") == "cancelled"
            )
            created_count = sum(1 for item in update_results if item.get("status") == "created")
            rescheduled_count = sum(
                len(item.get("task_ids", []))
                for item in update_results
                if item.get("status") == "rescheduled"
            )
            no_match_count = sum(1 for item in update_results if item.get("status") == "no_match")
            print(
                "[pupu] batch review task_updates processed: "
                f"input={len(task_updates)}, created={created_count}, "
                f"cancelled={cancelled_count}, rescheduled={rescheduled_count}, "
                f"no_match={no_match_count}"
            )
            for item in update_results[:6]:
                print(
                    "[pupu] batch review task_update result: "
                    f"action={item.get('action')} query={item.get('query')} "
                    f"status={item.get('status')} task_ids={item.get('task_ids', [])} "
                    f"reason={item.get('reason', '')}"
                )
        else:
            print("[pupu] batch review task_updates empty")

        if is_memu_long_term_enabled():
            if has_successful_memu_sync(
                context_session=context_session,
                identity_session=identity_session,
                start_msg_id=batch[0]["id"],
                end_msg_id=batch[-1]["id"],
            ):
                print(
                    "[pupu][memu] sync review skipped: "
                    f"already synced range={batch[0]['id']}..{batch[-1]['id']}"
                )
            else:
                memu_result = sync_review_memory(
                    context_session=context_session,
                    identity_session=identity_session,
                    start_msg_id=batch[0]["id"],
                    end_msg_id=batch[-1]["id"],
                    summary=summary,
                    person_facts=saved_person_facts,
                    event_threads=event_threads,
                )
                record_memu_sync(
                    context_session=context_session,
                    identity_session=identity_session,
                    start_msg_id=batch[0]["id"],
                    end_msg_id=batch[-1]["id"],
                    memu_ids=memu_result.ids,
                    status=memu_result.status,
                    error=memu_result.error,
                )
                print(
                    "[pupu][memu] sync review recorded: "
                    f"status={memu_result.status}, ids={len(memu_result.ids)}"
                )

        print(
            "[pupu] batch review done: "
            f"trigger={trigger}, messages_count={batch_turns}, summary_chars={len(summary)}, "
            f"familiarity_delta={familiarity_delta}, "
            f"person_facts={len(saved_person_facts)}, "
            f"event_updates={len(event_updates)}, "
            f"task_updates={len(task_updates)}"
        )
    except Exception as exc:
        print(f"[pupu] batch review failed: {exc}")
        print(traceback.format_exc())
