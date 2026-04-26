"""Core chat agent: builds prompts, calls model APIs, and persists conversation memory."""

import json
import os
from pathlib import Path
from datetime import datetime

import anthropic
from dotenv import load_dotenv

from .memory import (
    get_event_log,
    get_familiarity,
    get_messages_in_range,
    get_oldest_unsummarized_msg_id,
    get_recent_messages,
    get_self_facts,
    get_summaries,
    get_user_facts,
    save_message,
    save_summary,
    update_familiarity,
    upsert_self_facts,
    upsert_user_facts,
)
from .persona import (
    BATCH_REVIEW_PROMPT,
    build_system_prompt,
)
from .tools import TOOL_DEFINITIONS, execute_tool, is_admin_tool

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = "claude-opus-4-6"
JUDGE_MODEL = "claude-haiku-4-5-20251001"

_client = None


def _format_turn_timestamp() -> str:
    """Return a compact local timestamp for each user turn."""
    return datetime.now().strftime("%y%m%d-%H%M")


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        )
    return _client


def chat(user_input: str, session_id: str = "default", is_admin: bool = False, image_urls: list[str] = None, reply_speed_hint: str = None) -> str:
    """Process one turn of conversation. Returns the assistant's text reply."""
    client = _get_client()

    # Add image hint to text if real images are present
    display_text = user_input
    if image_urls:
        n = len(image_urls)
        hint = f"[用户发了{n}张图片]" if n > 1 else "[用户发了一张图片]"
        display_text = f"{hint} {user_input}" if user_input else hint

    # Add compact turn timestamp so the model knows when the user spoke.
    display_text = f"[t:{_format_turn_timestamp()}] {display_text}"

    # Save user message (text only for DB)
    save_message("user", display_text, session_id)

    # Build context
    history = get_recent_messages(50, session_id)
    score = get_familiarity(session_id)
    events = get_event_log(20, session_id)
    user_facts = get_user_facts(session_id)
    self_facts = get_self_facts(session_id)
    summaries = get_summaries(session_id, limit=5)
    system_prompt = build_system_prompt(score, events, user_facts, summaries, self_facts, reply_speed_hint)

    # Call API with tool use
    messages = [{"role": m["role"], "content": m["content"]} for m in history]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=messages,
        tools=TOOL_DEFINITIONS,
    )

    # Handle tool use loop
    while response.stop_reason == "tool_use":
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if is_admin_tool(block.name) and not is_admin:
                    result = "权限不足：只有管理员才能使用文件和命令工具。"
                else:
                    result = execute_tool(
                        block.name,
                        block.input,
                        image_urls=image_urls,
                        session_id=session_id,
                    )
                # Tool result content can be string or list of content blocks (for images)
                if isinstance(result, list):
                    content = result
                else:
                    content = result
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

    # Extract final text
    final_text = ""
    for block in response.content:
        if block.type == "text":
            final_text += block.text

    # Save assistant reply
    save_message("assistant", final_text, session_id)

    # Periodic batch review (every REVIEW_INTERVAL messages)
    _maybe_batch_review(client, session_id)

    return final_text


REVIEW_INTERVAL = 8


def _maybe_batch_review(client, session_id: str = "default"):
    """Every ~12 messages, do a single API call to summarize + judge familiarity + extract facts."""
    try:
        last_reviewed = get_oldest_unsummarized_msg_id(session_id)
        unsummarized = get_messages_in_range(session_id, last_reviewed, limit=200)

        if len(unsummarized) < REVIEW_INTERVAL:
            return

        batch = unsummarized[:REVIEW_INTERVAL]
        conversation_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '仆仆'}: {m['content']}"
            for m in batch
        )

        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=1024,
            system=BATCH_REVIEW_PROMPT,
            messages=[{"role": "user", "content": conversation_text}],
        )
        raw_text = response.content[0].text
        if not isinstance(raw_text, str):
            print(f"[pupu] batch review: unexpected response type {type(raw_text)}")
            return
        raw = raw_text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)

        summary = result.get("summary", "")
        if summary:
            save_summary(summary, batch[0]["id"], batch[-1]["id"], session_id)

        for event in result.get("familiarity_events", []):
            delta = int(event["delta"])
            reason = event["reason"]
            update_familiarity(delta, reason, session_id)

        user_facts = result.get("user_facts", {})
        if user_facts and isinstance(user_facts, dict):
            upsert_user_facts(user_facts, session_id)

        self_facts = result.get("self_facts", {})
        if self_facts and isinstance(self_facts, dict):
            upsert_self_facts(self_facts, session_id)

        print(f"[pupu] batch review done: {len(batch)} msgs, summary={len(summary)}chars, events={len(result.get('familiarity_events', []))}")
    except Exception as e:
        print(f"[pupu] batch review failed: {e}")
