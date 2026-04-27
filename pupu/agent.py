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
    get_important_events,
    get_oldest_unsummarized_msg_id,
    get_pending_review_last_message_time,
    get_recent_messages,
    get_review_candidate_batch,
    get_self_facts,
    get_summaries,
    get_user_facts,
    list_scheduled_tasks,
    list_pending_review_sessions,
    save_message,
    save_summary,
    update_familiarity,
    upsert_self_facts,
    upsert_user_facts,
)
from .persona import BATCH_REVIEW_PROMPT, build_system_prompt
from .review_followups import (
    apply_review_task_updates,
    normalize_review_important_events,
    normalize_review_task_drafts,
    normalize_review_task_updates,
    save_review_important_events,
)
from .tools import TOOL_DEFINITIONS, execute_tool, is_admin_tool

REVIEW_INTERVAL = 8
REVIEW_IDLE_SECONDS = 600
REVIEW_SOURCE = "chat"
CHAT_HISTORY_LIMIT = 30
PROMPT_SUMMARY_LIMIT = 3
PROMPT_IMPORTANT_EVENT_LIMIT = 6
BATCH_REVIEW_MAX_TOKENS = 768
REVIEW_TASK_CONTEXT_LIMIT = 30
REVIEW_TASK_FIELD_LIMIT = 120
_batch_review_lock = threading.Lock()


def _format_turn_timestamp() -> str:
    """Return a compact local timestamp for each user turn."""
    return datetime.now().strftime("%y%m%d-%H%M")


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
    """Best-effort repair for model JSON that forgot to escape quotes inside strings."""
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


def _normalize_fact_map(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    cleaned = {}
    for key, fact_value in value.items():
        key_text = str(key).strip()
        value_text = str(fact_value).strip()
        if key_text and value_text:
            cleaned[key_text] = value_text
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
            "user_facts": {},
            "self_facts": {},
            "important_events": [],
            "task_updates": [],
        }

    task_updates = normalize_review_task_updates(value.get("task_updates", []))
    if not task_updates:
        legacy_drafts = normalize_review_task_drafts(value.get("task_drafts", []))
        for draft in legacy_drafts:
            if not draft.get("should_create"):
                continue
            task_updates.append(
                {
                    "action": "create",
                    "query": "",
                    "source_event_key": draft.get("source_event_key", ""),
                    "title": draft.get("title", ""),
                    "instruction": draft.get("instruction", ""),
                    "run_at": draft.get("run_at", ""),
                    "repeat": draft.get("repeat", "once"),
                    "interval_seconds": draft.get("interval_seconds"),
                    "kind": draft.get("kind", ""),
                    "reason": "legacy_task_draft",
                }
            )

    return {
        "summary": str(value.get("summary", "")).strip(),
        "familiarity_delta": _normalize_familiarity_delta(
            value.get("familiarity_delta", 0)
        ),
        "user_facts": _normalize_fact_map(value.get("user_facts", {})),
        "self_facts": _normalize_fact_map(value.get("self_facts", {})),
        "important_events": normalize_review_important_events(
            value.get("important_events", [])
        ),
        "task_updates": task_updates,
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


def _build_fallback_summary(batch: list[dict]) -> str:
    turn_snippets = []
    current_turn = []
    for message in batch:
        speaker = "User" if message["role"] == "user" else "Pupu"
        text = " ".join(str(message["content"]).split())
        current_turn.append(f"{speaker}:{text[:80]}")
        if message["role"] == "assistant":
            turn_snippets.append(" / ".join(current_turn))
            current_turn = []

    if current_turn:
        turn_snippets.append(" / ".join(current_turn))

    if not turn_snippets:
        return (
            "A few sparse interactions happened in this batch, "
            "but there was not enough content to summarize cleanly."
        )

    preview = " ; ".join(turn_snippets[:4])
    if len(turn_snippets) > 4:
        preview += f" ; plus {len(turn_snippets) - 4} more turns"
    return f"Conversation batch summary: {preview}"[:220]


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


def _seconds_since_iso(value: str | None, now: datetime | None = None) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone().replace(tzinfo=None)
    except Exception:
        return None
    current = now or datetime.now()
    return max(0, int((current - moment).total_seconds()))


def chat(
    user_input: str,
    session_id: str = "default",
    is_admin: bool = False,
    image_urls: list[str] = None,
    reply_speed_hint: str = None,
    message_source: str = REVIEW_SOURCE,
) -> str:
    """Process one turn of conversation. Returns the assistant's text reply."""
    display_text = user_input
    if image_urls:
        n = len(image_urls)
        hint = f"[用户发了{n}张图片]" if n > 1 else "[用户发了一张图片]"
        display_text = f"{hint} {user_input}" if user_input else hint

    display_text = f"[t:{_format_turn_timestamp()}] {display_text}"

    save_message("user", display_text, session_id, source=message_source)

    history = get_recent_messages(CHAT_HISTORY_LIMIT, session_id)
    score = get_familiarity(session_id)
    user_facts = get_user_facts(session_id)
    self_facts = get_self_facts(session_id)
    summaries = get_summaries(session_id, limit=PROMPT_SUMMARY_LIMIT)
    important_events = get_important_events(session_id, limit=PROMPT_IMPORTANT_EVENT_LIMIT)
    system_prompt = build_system_prompt(
        score,
        None,
        user_facts,
        summaries,
        self_facts,
        important_events,
        reply_speed_hint,
    )

    messages = [{"role": m["role"], "content": m["content"]} for m in history]

    def _tool_handler(tool_name: str, tool_input: dict, reason_hint: str | None = None):
        if is_admin_tool(tool_name) and not is_admin:
            return "权限不足：只有管理员才能使用文件和命令工具。"
        return execute_tool(
            tool_name,
            tool_input,
            image_urls=image_urls,
            session_id=session_id,
            reason_hint=reason_hint or None,
        )

    final_text = chat_complete(
        role="chat",
        model=MODEL,
        system=system_prompt,
        messages=messages,
        max_tokens=2048,
        tools=TOOL_DEFINITIONS,
        tool_handler=_tool_handler,
        session_id=session_id,
        image_urls=image_urls,
        is_admin=is_admin,
        tool_exposure="chat",
    )
    save_message("assistant", final_text, session_id, source=message_source)

    if message_source == REVIEW_SOURCE:
        _maybe_batch_review(session_id)

    return final_text


def _maybe_batch_review(session_id: str = "default"):
    if not _batch_review_lock.acquire(blocking=False):
        print(f"[pupu] batch review skip: lock busy session={session_id}")
        return
    try:
        return _maybe_batch_review_unlocked(session_id)
    finally:
        _batch_review_lock.release()


def run_due_batch_reviews():
    for session_id in list_pending_review_sessions(REVIEW_SOURCE):
        _maybe_batch_review(session_id)


def _maybe_batch_review_unlocked(session_id: str = "default"):
    """Every REVIEW_INTERVAL completed chat turns, summarize + judge familiarity + extract facts."""
    try:
        print(
            "[pupu] batch review check: "
            f"session={session_id}, interval={REVIEW_INTERVAL}, idle={REVIEW_IDLE_SECONDS}s"
        )
        last_reviewed = get_oldest_unsummarized_msg_id(session_id)
        pending_turns = count_pending_review_turns(
            session_id=session_id,
            after_msg_id=last_reviewed,
            source=REVIEW_SOURCE,
        )
        last_pending_time = get_pending_review_last_message_time(
            session_id=session_id,
            after_msg_id=last_reviewed,
            source=REVIEW_SOURCE,
        )
        idle_seconds = _seconds_since_iso(last_pending_time)
        print(
            "[pupu] batch review context: "
            f"last_reviewed_id={last_reviewed}, pending_turns={pending_turns}, "
            f"idle_seconds={idle_seconds}"
        )

        trigger = ""
        review_turns = 0
        if pending_turns >= REVIEW_INTERVAL:
            trigger = "turns"
            review_turns = REVIEW_INTERVAL
        elif (
            pending_turns > 0
            and idle_seconds is not None
            and idle_seconds >= REVIEW_IDLE_SECONDS
        ):
            trigger = "idle"
            review_turns = pending_turns

        if not trigger:
            print(
                "[pupu] batch review skip: "
                f"need={REVIEW_INTERVAL}, got={pending_turns}, "
                f"idle_need={REVIEW_IDLE_SECONDS}, idle_got={idle_seconds}"
            )
            return

        batch = get_review_candidate_batch(
            session_id=session_id,
            review_interval=review_turns,
            source=REVIEW_SOURCE,
            min_turns=review_turns,
        )
        if not batch:
            print("[pupu] batch review skip: candidate batch unavailable")
            return

        batch_turns = sum(1 for item in batch if item["role"] == "assistant")
        print(
            "[pupu] batch review trigger: "
            f"trigger={trigger}, turns={batch_turns}, pending_turns={pending_turns}, "
            f"idle_seconds={idle_seconds}, messages={len(batch)}, "
            f"msg_id_range={batch[0]['id']}..{batch[-1]['id']}"
        )

        active_tasks_text = _format_active_scheduled_tasks_for_review(session_id)
        conversation_text = "\n".join(
            f"{'User' if item['role'] == 'user' else 'Pupu'}: {item['content']}"
            for item in batch
        )
        conversation_text = (
            f"Current local time: {datetime.now().isoformat(timespec='seconds')}\n\n"
            + active_tasks_text
            + "\n\n待整理对话：\n"
            + conversation_text
        )
        print(
            "[pupu] batch review request: "
            f"provider={provider_label('judge', JUDGE_MODEL)}, input_chars={len(conversation_text)}"
        )

        raw_text = json_task(
            role="judge",
            model=JUDGE_MODEL,
            system=BATCH_REVIEW_PROMPT,
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
                "summary": _build_fallback_summary(batch),
                "familiarity_delta": 0,
                "user_facts": {},
                "self_facts": {},
                "important_events": [],
                "task_updates": [],
            }
            print("[pupu] batch review fallback summary enabled")

        summary = result.get("summary") or _build_fallback_summary(batch)
        save_summary(summary, batch[0]["id"], batch[-1]["id"], session_id)
        print(
            "[pupu] batch review summary saved: "
            f"chars={len(summary)}, range={batch[0]['id']}..{batch[-1]['id']}"
        )

        familiarity_delta = int(result.get("familiarity_delta", 0) or 0)
        if familiarity_delta:
            update_familiarity(familiarity_delta, session_id=session_id)
            print(
                "[pupu] batch review familiarity delta applied: "
                f"delta={familiarity_delta}"
            )
        else:
            print("[pupu] batch review familiarity delta empty")

        user_facts = result.get("user_facts", {})
        if user_facts:
            upsert_user_facts(user_facts, session_id)
            print(
                "[pupu] batch review user_facts upserted: "
                f"count={len(user_facts)}, keys={list(user_facts.keys())}"
            )
        else:
            print("[pupu] batch review user_facts empty")

        self_facts = result.get("self_facts", {})
        if self_facts:
            upsert_self_facts(self_facts, session_id)
            print(
                "[pupu] batch review self_facts upserted: "
                f"count={len(self_facts)}, keys={list(self_facts.keys())}"
            )
        else:
            print("[pupu] batch review self_facts empty")

        important_events = result.get("important_events", [])
        saved_event_rows = {}
        if important_events:
            saved_event_rows = save_review_important_events(session_id, important_events)
            print(
                "[pupu] batch review important_events saved: "
                f"count={len(saved_event_rows)}, keys={list(saved_event_rows.keys())[:6]}"
            )
        else:
            print("[pupu] batch review important_events empty")

        task_updates = result.get("task_updates", [])
        if task_updates:
            update_results = apply_review_task_updates(
                session_id,
                task_updates,
                saved_event_rows,
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

        print(
            "[pupu] batch review done: "
            f"trigger={trigger}, turns={batch_turns}, summary_chars={len(summary)}, "
            f"familiarity_delta={familiarity_delta}, "
            f"important_events={len(important_events)}, "
            f"task_updates={len(task_updates)}"
        )
    except Exception as exc:
        print(f"[pupu] batch review failed: {exc}")
        print(traceback.format_exc())
