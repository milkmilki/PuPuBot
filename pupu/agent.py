"""Core chat agent: builds prompts, calls model APIs, and persists conversation memory."""

import json
import re
import traceback
from datetime import datetime

from .llm import JUDGE_MODEL, MODEL, collect_reason_hint, get_client, join_text_blocks
from .memory import (
    count_pending_review_turns,
    get_event_log,
    get_familiarity,
    get_oldest_unsummarized_msg_id,
    get_recent_messages,
    get_review_candidate_batch,
    get_self_facts,
    get_summaries,
    get_user_facts,
    save_message,
    save_summary,
    update_familiarity,
    upsert_self_facts,
    upsert_user_facts,
)
from .persona import BATCH_REVIEW_PROMPT, build_system_prompt
from .tools import TOOL_DEFINITIONS, execute_tool, is_admin_tool

REVIEW_INTERVAL = 8
REVIEW_SOURCE = "chat"
_get_client = get_client


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


def _normalize_familiarity_events(value) -> list[dict[str, int | str]]:
    if not isinstance(value, list):
        return []

    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            delta = int(item.get("delta", 0))
        except Exception:
            continue
        reason = str(item.get("reason", "")).strip()
        if delta == 0 or not reason:
            continue
        cleaned.append({"delta": delta, "reason": reason})
    return cleaned


def _normalize_batch_review_result(value) -> dict:
    if not isinstance(value, dict):
        return {
            "summary": "",
            "familiarity_events": [],
            "user_facts": {},
            "self_facts": {},
        }

    return {
        "summary": str(value.get("summary", "")).strip(),
        "familiarity_events": _normalize_familiarity_events(
            value.get("familiarity_events", [])
        ),
        "user_facts": _normalize_fact_map(value.get("user_facts", {})),
        "self_facts": _normalize_fact_map(value.get("self_facts", {})),
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


def chat(
    user_input: str,
    session_id: str = "default",
    is_admin: bool = False,
    image_urls: list[str] = None,
    reply_speed_hint: str = None,
    message_source: str = REVIEW_SOURCE,
) -> str:
    """Process one turn of conversation. Returns the assistant's text reply."""
    client = get_client()

    display_text = user_input
    if image_urls:
        n = len(image_urls)
        hint = f"[用户发了{n}张图片]" if n > 1 else "[用户发了一张图片]"
        display_text = f"{hint} {user_input}" if user_input else hint

    display_text = f"[t:{_format_turn_timestamp()}] {display_text}"

    save_message("user", display_text, session_id, source=message_source)

    history = get_recent_messages(50, session_id)
    score = get_familiarity(session_id)
    events = get_event_log(20, session_id)
    user_facts = get_user_facts(session_id)
    self_facts = get_self_facts(session_id)
    summaries = get_summaries(session_id, limit=5)
    system_prompt = build_system_prompt(
        score,
        events,
        user_facts,
        summaries,
        self_facts,
        reply_speed_hint,
    )

    messages = [{"role": m["role"], "content": m["content"]} for m in history]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=messages,
        tools=TOOL_DEFINITIONS,
    )

    while response.stop_reason == "tool_use":
        tool_results = []
        reason_hint = collect_reason_hint(response.content)

        for block in response.content:
            if block.type != "tool_use":
                continue

            if is_admin_tool(block.name) and not is_admin:
                result = "权限不足：只有管理员才能使用文件和命令工具。"
            else:
                result = execute_tool(
                    block.name,
                    block.input,
                    image_urls=image_urls,
                    session_id=session_id,
                    reason_hint=reason_hint or None,
                )

            content = result if not isinstance(result, list) else result
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                }
            )

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=messages,
            tools=TOOL_DEFINITIONS,
        )

    final_text = join_text_blocks(response.content)
    save_message("assistant", final_text, session_id, source=message_source)

    if message_source == REVIEW_SOURCE:
        _maybe_batch_review(client, session_id)

    return final_text


def _maybe_batch_review(client, session_id: str = "default"):
    """Every REVIEW_INTERVAL completed chat turns, summarize + judge familiarity + extract facts."""
    try:
        print(
            f"[pupu] batch review check: session={session_id}, interval={REVIEW_INTERVAL}"
        )
        last_reviewed = get_oldest_unsummarized_msg_id(session_id)
        pending_turns = count_pending_review_turns(
            session_id=session_id,
            after_msg_id=last_reviewed,
            source=REVIEW_SOURCE,
        )
        print(
            "[pupu] batch review context: "
            f"last_reviewed_id={last_reviewed}, pending_turns={pending_turns}"
        )

        if pending_turns < REVIEW_INTERVAL:
            print(
                "[pupu] batch review skip: "
                f"need={REVIEW_INTERVAL}, got={pending_turns}"
            )
            return

        batch = get_review_candidate_batch(
            session_id=session_id,
            review_interval=REVIEW_INTERVAL,
            source=REVIEW_SOURCE,
        )
        if not batch:
            print("[pupu] batch review skip: candidate batch unavailable")
            return

        batch_turns = sum(1 for item in batch if item["role"] == "assistant")
        print(
            "[pupu] batch review trigger: "
            f"turns={batch_turns}, messages={len(batch)}, "
            f"msg_id_range={batch[0]['id']}..{batch[-1]['id']}"
        )

        conversation_text = "\n".join(
            f"{'User' if item['role'] == 'user' else 'Pupu'}: {item['content']}"
            for item in batch
        )
        print(
            "[pupu] batch review request: "
            f"model={JUDGE_MODEL}, input_chars={len(conversation_text)}"
        )

        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=1024,
            system=BATCH_REVIEW_PROMPT,
            messages=[{"role": "user", "content": conversation_text}],
        )
        usage = getattr(response, "usage", None)
        stop_reason = getattr(response, "stop_reason", None)
        print(
            "[pupu] batch review response: "
            f"stop_reason={stop_reason}, usage={usage}"
        )

        raw_text = response.content[0].text
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
                "familiarity_events": [],
                "user_facts": {},
                "self_facts": {},
            }
            print("[pupu] batch review fallback summary enabled")

        summary = result.get("summary") or _build_fallback_summary(batch)
        save_summary(summary, batch[0]["id"], batch[-1]["id"], session_id)
        print(
            "[pupu] batch review summary saved: "
            f"chars={len(summary)}, range={batch[0]['id']}..{batch[-1]['id']}"
        )

        familiarity_events = result.get("familiarity_events", [])
        for idx, event in enumerate(familiarity_events, start=1):
            delta = int(event["delta"])
            reason = str(event["reason"]).strip()
            update_familiarity(delta, reason, session_id)
            print(
                "[pupu] batch review familiarity event "
                f"#{idx}: delta={delta}, reason={reason}"
            )
        if not familiarity_events:
            print("[pupu] batch review familiarity events empty")

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

        print(
            "[pupu] batch review done: "
            f"turns={batch_turns}, summary_chars={len(summary)}, "
            f"events={len(familiarity_events)}"
        )
    except Exception as exc:
        print(f"[pupu] batch review failed: {exc}")
        print(traceback.format_exc())
