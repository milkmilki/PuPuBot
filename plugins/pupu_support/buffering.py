"""Debounce and message buffering for interactive chat sessions."""

from __future__ import annotations

import asyncio

from pupu.agent import chat
from pupu.config import load_first_numeric_owner_id
from pupu.dialogue_loop import cancel_wait_timer, is_followup_eligible, register_sender
from pupu.familiarity import compute_reply_delay
from pupu.memory import get_familiarity, save_message

from . import state
from .common import (
    compute_reply_speed_hint,
    log,
    send_private_segments,
    send_segments,
    split_message,
)


def _make_qq_wait_followup_sender(bot, sid: str, loop: asyncio.AbstractEventLoop):
    """Build a sync sender for wait_followup delivery (private / owner sessions)."""
    uid = None
    if sid == state.OWNER_SESSION:
        uid = load_first_numeric_owner_id()
    elif sid.startswith("private_"):
        tail = sid[8:]
        if tail.isdigit():
            uid = int(tail)
    if uid is None:
        return None

    async def _async_send(text: str):
        segs = split_message(text)
        await send_private_segments(bot, uid, segs)
        log("send", "私聊", str(uid), text)

    def _send(text: str):
        fut = asyncio.run_coroutine_threadsafe(_async_send(text), loop)
        try:
            fut.result(timeout=120)
        except Exception as exc:
            print(f"[pupu] wait_followup send failed session={sid} err={exc}")

    return _send


def register_owner_wait_followup_sender(bot, loop: asyncio.AbstractEventLoop) -> None:
    """Register owner session sender when the bot connects (for proactive + timer delivery)."""
    sender = _make_qq_wait_followup_sender(bot, state.OWNER_SESSION, loop)
    if sender:
        register_sender(state.OWNER_SESSION, sender)


async def buffer_message(
    sid: str,
    text: str,
    image_urls: list[str],
    bot,
    event,
    is_admin: bool,
    nickname: str,
    session_label: str,
    reply_prefix=None,
):
    if sid not in state.msg_buffers:
        state.msg_buffers[sid] = {
            "texts": [],
            "image_urls": [],
            "bot": bot,
            "event": event,
            "is_admin": is_admin,
            "nickname": nickname,
            "session_label": session_label,
            "reply_prefix": reply_prefix,
        }

    buf = state.msg_buffers[sid]

    try:
        if cancel_wait_timer(sid):
            print(f"[pupu] wait_followup timer cancelled: session={sid}")
    except Exception as exc:
        print(f"[pupu] wait_followup cancel failed: session={sid} error={exc}")

    if is_followup_eligible(sid):
        loop = asyncio.get_running_loop()
        sender = _make_qq_wait_followup_sender(bot, sid, loop)
        if sender:
            register_sender(sid, sender)

    if text:
        buf["texts"].append(text)
    buf["image_urls"].extend(image_urls)
    buf["bot"] = bot
    buf["event"] = event
    if reply_prefix is not None:
        buf["reply_prefix"] = reply_prefix

    phase = state.session_phase.get(sid)
    if phase in ("delaying", "processing"):
        return

    if sid in state.debounce_tasks:
        state.debounce_tasks[sid].cancel()

    state.debounce_tasks[sid] = asyncio.create_task(debounce_flush(sid))


async def debounce_flush(sid: str):
    try:
        await asyncio.sleep(state.DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    score = get_familiarity(sid)
    delay, replacement = compute_reply_delay(score)

    if replacement is not None:
        print(f"[pupu] 敷衍回复: {replacement}")
        buf = state.msg_buffers.pop(sid, None)
        state.debounce_tasks.pop(sid, None)
        state.session_phase.pop(sid, None)
        if buf:
            combined = "\n".join(text for text in buf["texts"] if text)
            if combined:
                save_message("user", combined, sid)
            save_message("assistant", replacement, sid)
            log("recv", buf["session_label"], buf["nickname"], combined or "[图片]")
            log("send", buf["session_label"], buf["nickname"], replacement)
            segments = split_message(replacement)
            await send_segments(
                buf["bot"],
                buf["event"],
                segments,
                prefix=buf.get("reply_prefix"),
            )
        return

    if delay > 0:
        state.session_phase[sid] = "delaying"
        print(f"[pupu] 延迟回复: {delay:.1f}秒 (好感度{score})")
        await asyncio.sleep(delay)
        state.session_phase[sid] = "buffering"
        await asyncio.sleep(state.DEBOUNCE_SECONDS)

    state.session_phase[sid] = "processing"
    buf = state.msg_buffers.pop(sid, None)
    state.debounce_tasks.pop(sid, None)

    if not buf:
        state.session_phase.pop(sid, None)
        return

    combined_text = "\n".join(text for text in buf["texts"] if text)
    image_urls = buf["image_urls"]

    if not combined_text and not image_urls:
        state.session_phase.pop(sid, None)
        return

    try:
        log("recv", buf["session_label"], buf["nickname"], combined_text or "[图片]")
        speed_hint = compute_reply_speed_hint(sid)
        reply = await asyncio.to_thread(
            chat,
            combined_text,
            sid,
            buf["is_admin"],
            image_urls or None,
            speed_hint,
        )
        log("send", buf["session_label"], buf["nickname"], reply)
        segments = split_message(reply)
        await send_segments(
            buf["bot"],
            buf["event"],
            segments,
            prefix=buf.get("reply_prefix"),
        )
    except Exception as exc:
        print(f"[pupu] flush error ({sid}): {exc}")
        try:
            await buf["bot"].send(buf["event"], "呃，脑子卡了一下")
        except Exception:
            pass
    finally:
        state.session_phase.pop(sid, None)
        if sid in state.msg_buffers and sid not in state.debounce_tasks:
            state.debounce_tasks[sid] = asyncio.create_task(debounce_flush(sid))
