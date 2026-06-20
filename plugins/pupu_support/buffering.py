"""Debounce and message buffering for interactive chat sessions.

For **open groups** the speaker decision is now driven by the centralized
arbiter:

  1. Each inbound group message is buffered locally (so the selected bot can
     compose a coherent reply) **and** pushed to ``POST /api/observe``.
  2. A per-group ``arbiter_decision_subscriber`` long-polls
     ``GET /api/await_decision``. When the arbiter announces a decision and
     this bot is the chosen speaker, ``act_as_selected_speaker`` runs.
  3. After sending, the bot's own reply is also pushed via ``observe`` so
     the next round's context is up to date.

For **private/owner** chats the legacy local debounce path (``debounce_flush``)
is kept unchanged.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from pupu.agent import _format_turn_timestamp, chat
from pupu.config import (
    load_arbiter_base_url,
    load_arbiter_subscribe_timeout_seconds,
    load_arbiter_timeout_seconds,
    load_bot_id,
    load_config,
    load_first_numeric_owner_id,
    load_max_consecutive_bot_turns,
    load_open_group_debounce_seconds,
    load_peer_config,
)
from pupu.dialogue_loop import cancel_wait_timer, is_followup_eligible, register_sender
from pupu.memory import save_message_with_speaker
from pupu.storage.people import resolve_person_for_prompt

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


def _configured_persona_brief() -> str:
    try:
        cfg = load_config()
    except Exception:
        return ""
    return str(cfg.get("display_name") or cfg.get("name") or cfg.get("bot_id") or "").strip()


def _bot_log_name(bot=None) -> str:
    return (
        _configured_persona_brief()
        or str(getattr(bot, "self_id", "") or "").strip()
        or load_bot_id()
        or "bot"
    )


def _identity_session_for_context(sid: str, identity_session: str | None = None) -> str:
    if str(sid or "").startswith("group_"):
        return state.OWNER_SESSION
    return str(identity_session or sid)


def _with_turn_timestamp(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return value
    if value.startswith("[时间:"):
        return value
    return f"[时间: {_format_turn_timestamp()}] {value}"


def _at_targets(text: str) -> list[str]:
    return sorted(set(re.findall(r"@(\d{5,})", text or "")))


def _is_command_text(text: str) -> bool:
    return bool(str(text or "").lstrip().startswith("/"))


_SPEAKER_PREFIX_RE = re.compile(r"^\s*\[(?:bot\s+)?(?P<name>.+?)\(QQ:(?P<qq>\d+)\)\]\s*(?P<text>.*)$")


def _canonical_speaker(
    *,
    speaker_key: str = "",
    speaker_user_id: str = "",
    speaker_name: str = "",
    speaker_is_bot: bool = False,
) -> dict:
    person = resolve_person_for_prompt(
        person_key=speaker_key,
        qq_id=speaker_user_id,
        display_name=speaker_name,
        kind="qq" if speaker_user_id else "user",
    )
    if speaker_is_bot:
        person["kind"] = "bot"
    return person


def _strip_speaker_prefix(text: str) -> str:
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        match = _SPEAKER_PREFIX_RE.match(raw_line)
        lines.append(match.group("text") if match else raw_line)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Centralized arbiter integration (open groups)
# ---------------------------------------------------------------------------


def _arbiter_observe_url() -> str:
    return f"{load_arbiter_base_url().rstrip('/')}/api/observe"


def _arbiter_await_url() -> str:
    return f"{load_arbiter_base_url().rstrip('/')}/api/await_decision"


def _build_observe_payload(buf: dict, *, bot, text: str, message_id: str) -> dict:
    bot_id = load_bot_id() or str(getattr(bot, "self_id", "") or "bot")
    group_id = str(buf.get("group_id") or "")
    person = _canonical_speaker(
        speaker_key=str(buf.get("speaker_key") or ""),
        speaker_user_id=str(buf.get("speaker_user_id") or ""),
        speaker_name=str(buf.get("speaker_name") or ""),
        speaker_is_bot=bool(buf.get("speaker_is_bot")),
    )
    return {
        "group_id": group_id,
        "message_id": message_id,
        "speaker_qq": str(buf.get("speaker_user_id") or ""),
        "speaker_name": str(person.get("display_name") or buf.get("speaker_name") or ""),
        "speaker_person_key": str(person.get("person_key") or ""),
        "speaker_is_bot": bool(buf.get("speaker_is_bot")),
        "text": _strip_speaker_prefix(text),
        "ts": "",
        "reporter": {
            "bot_id": bot_id,
            "qq": str(getattr(bot, "self_id", "") or ""),
            "name": bot_id,
            "persona_brief": _configured_persona_brief(),
            "min_bot_gap_seconds": 10,
            "max_consecutive_bot_turns": load_max_consecutive_bot_turns(),
        },
        "peers": [load_peer_config()] if load_peer_config() else [],
    }


async def _post_observe_async(payload: dict) -> dict | None:
    """Best-effort POST to /api/observe; never raises."""
    url = _arbiter_observe_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
    except Exception as exc:
        print(
            f"[pupu][arbiter] observe failed group={payload.get('group_id')} "
            f"msg={payload.get('message_id')} err={type(exc).__name__}: {exc}"
        )
        return None


def _ensure_arbiter_subscriber(
    *, group_id: str, sid: str, bot, initial_since: int | None
) -> None:
    """Start (idempotently) the per-group decision subscriber task."""
    if not group_id:
        return
    existing = state.arbiter_subscriber_tasks.get(group_id)
    if existing and not existing.done():
        return
    if initial_since is not None:
        # First ever message: skip whatever's already in DB and only react to
        # decisions produced from THIS round on.
        state.arbiter_last_decision_id.setdefault(group_id, int(initial_since))
    state.arbiter_subscriber_tasks[group_id] = asyncio.create_task(
        arbiter_decision_subscriber(group_id, sid, bot)
    )


async def arbiter_decision_subscriber(group_id: str, sid: str, bot) -> None:
    bot_id = load_bot_id() or str(getattr(bot, "self_id", "") or "bot")
    timeout_sec = load_arbiter_subscribe_timeout_seconds()
    backoff = 1.0
    while True:
        try:
            since = int(state.arbiter_last_decision_id.get(group_id, 0))
            url = _arbiter_await_url()
            params = {
                "group_id": group_id,
                "since": str(since),
                "timeout": str(timeout_sec),
            }
            async with httpx.AsyncClient(timeout=timeout_sec + 10.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                body = response.json()
            decision = (body or {}).get("decision")
            if not decision:
                # Long-poll timed out without a new decision.
                backoff = 1.0
                continue
            decision_id = int(decision.get("decision_id") or 0)
            speaker = str(decision.get("speaker") or "none")
            reason = str(decision.get("reason") or "")
            confidence = float(decision.get("confidence") or 0.0)
            print(
                "[pupu][arbiter] decision "
                f"group={group_id} decision_id={decision_id} me={bot_id} "
                f"speaker={speaker} reason={reason} conf={confidence:.2f}"
            )
            state.arbiter_last_decision_id[group_id] = decision_id
            backoff = 1.0
            if speaker != bot_id:
                continue
            phase = state.session_phase.get(sid)
            if phase == "processing":
                print(
                    f"[pupu][arbiter] skip act: session={sid} phase={phase} "
                    f"decision_id={decision_id}"
                )
                continue
            buf = state.msg_buffers.get(sid)
            if not buf:
                print(
                    f"[pupu][arbiter] selected but no buffered text yet, skip "
                    f"session={sid} decision_id={decision_id}"
                )
                continue
            asyncio.create_task(act_as_selected_speaker(sid))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            print(
                f"[pupu][arbiter] subscriber error group={group_id} "
                f"err={type(exc).__name__}: {exc}"
            )
            try:
                await asyncio.sleep(min(backoff, 15.0))
            except asyncio.CancelledError:
                return
            backoff = min(backoff * 2.0, 15.0)


async def _post_self_reply_observe(buf: dict, bot, text: str) -> None:
    """Push the bot's own reply back into the arbiter so future rounds see it."""
    if not text:
        return
    group_id = str(buf.get("group_id") or "")
    if not group_id:
        return
    bot_id = load_bot_id() or str(getattr(bot, "self_id", "") or "bot")
    self_name = _configured_persona_brief() or bot_id
    payload = {
        "group_id": group_id,
        "message_id": f"self:{bot_id}:{time.time_ns()}",
        "speaker_qq": str(getattr(bot, "self_id", "") or ""),
        "speaker_name": self_name,
        "speaker_person_key": "instance",
        "speaker_is_bot": True,
        "text": text,
        "ts": "",
        "reporter": {
            "bot_id": bot_id,
            "qq": str(getattr(bot, "self_id", "") or ""),
            "name": bot_id,
            "persona_brief": _configured_persona_brief(),
            "min_bot_gap_seconds": 10,
            "max_consecutive_bot_turns": load_max_consecutive_bot_turns(),
        },
    }
    await _post_observe_async(payload)


# ---------------------------------------------------------------------------
# Selected-speaker action (open groups)
# ---------------------------------------------------------------------------


async def act_as_selected_speaker(sid: str) -> None:
    """Generate and send a reply for an open-group session that just won arbitration.

    Mirrors the behaviour of the legacy ``debounce_flush`` for open groups,
    but is triggered by the arbiter subscriber rather than a local timer.
    """
    buf = state.msg_buffers.get(sid)
    if not buf:
        return
    if state.session_phase.get(sid) == "processing":
        return

    state.session_phase[sid] = "processing"
    buf = state.msg_buffers.pop(sid, None)
    if buf is None:
        state.session_phase.pop(sid, None)
        return

    combined_text = "\n".join(text for text in buf.get("texts") or [] if text)
    image_urls = list(buf.get("image_urls") or [])
    identity_session = _identity_session_for_context(sid, str(buf.get("identity_session") or ""))

    if not combined_text and not image_urls:
        state.session_phase.pop(sid, None)
        return

    try:
        log("recv", buf.get("session_label") or "", buf.get("nickname") or "", combined_text or "[图片]")
        if combined_text:
            speaker_payload = json.dumps(buf.get("speakers") or [], ensure_ascii=False)
            save_message_with_speaker(
                "user",
                _with_turn_timestamp(combined_text),
                sid,
                speaker_key=speaker_payload,
                speaker_name=str(buf.get("speaker_name") or buf.get("nickname") or ""),
                speaker_qq=str(buf.get("speaker_user_id") or ""),
            )

        speed_hint = compute_reply_speed_hint(sid)
        speaker_payload = json.dumps(buf.get("speakers") or [], ensure_ascii=False)
        reply = await asyncio.to_thread(
            chat,
            combined_text,
            sid,
            bool(buf.get("is_admin")),
            image_urls or None,
            speed_hint,
            context_session=sid,
            identity_session=identity_session,
            persist_user=False,
            speaker_key=speaker_payload,
            speaker_name=str(buf.get("speaker_name") or buf.get("nickname") or ""),
            speaker_qq=str(buf.get("speaker_user_id") or ""),
        )
        log("send", buf.get("session_label") or "", _bot_log_name(buf.get("bot")), reply)
        segments = split_message(reply)
        await send_segments(
            buf["bot"],
            buf["event"],
            segments,
            prefix=buf.get("reply_prefix"),
        )
        await _post_self_reply_observe(buf, buf["bot"], reply)
    except Exception as exc:
        print(f"[pupu] act_as_selected_speaker error ({sid}): {exc}")
        try:
            await buf["bot"].send(buf["event"], "呃，脑子卡了一下")
        except Exception:
            pass
    finally:
        state.session_phase.pop(sid, None)


# ---------------------------------------------------------------------------
# Buffer entry point + private/owner debounce
# ---------------------------------------------------------------------------


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
    identity_session: str | None = None,
    is_open_group: bool = False,
    group_id: str | None = None,
    message_id: str | None = None,
    speaker_key: str | None = None,
    speaker_user_id: str | None = None,
    speaker_name: str | None = None,
    speaker_is_bot: bool = False,
):
    if _is_command_text(text):
        return

    identity_session = _identity_session_for_context(sid, identity_session)
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
            "identity_session": identity_session,
            "is_open_group": is_open_group,
            "group_id": group_id,
            "last_message_id": message_id,
            "speakers": [],
            "speaker_key": speaker_key,
            "speaker_user_id": speaker_user_id,
            "speaker_name": speaker_name,
            "speaker_is_bot": speaker_is_bot,
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
    buf["identity_session"] = identity_session
    buf["is_open_group"] = bool(buf.get("is_open_group") or is_open_group)
    if group_id is not None:
        buf["group_id"] = group_id
    if message_id is not None:
        buf["last_message_id"] = message_id
    if speaker_user_id is not None:
        buf["speaker_user_id"] = speaker_user_id
    if speaker_name is not None:
        buf["speaker_name"] = speaker_name
    if speaker_key is not None:
        buf["speaker_key"] = speaker_key
    if speaker_key or speaker_user_id or speaker_name:
        canonical = _canonical_speaker(
            speaker_key=str(speaker_key or ""),
            speaker_user_id=str(speaker_user_id or ""),
            speaker_name=str(speaker_name or ""),
            speaker_is_bot=speaker_is_bot,
        )
        speaker = {
            "person_key": str(canonical.get("person_key") or speaker_key or "").strip(),
            "display_name": str(canonical.get("display_name") or speaker_name or speaker_user_id or "").strip(),
            "qq_id": str(speaker_user_id or "").strip(),
            "kind": str(canonical.get("kind") or ("qq" if speaker_user_id else "user")),
        }
        speakers = buf.setdefault("speakers", [])
        signature = (speaker["person_key"], speaker["qq_id"], speaker["display_name"])
        if not any(
            (
                str(item.get("person_key") or ""),
                str(item.get("qq_id") or ""),
                str(item.get("display_name") or ""),
            )
            == signature
            for item in speakers
            if isinstance(item, dict)
        ):
            speakers.append(speaker)
    buf["speaker_is_bot"] = bool(speaker_is_bot)
    if reply_prefix is not None:
        buf["reply_prefix"] = reply_prefix

    phase = state.session_phase.get(sid)

    if buf.get("is_open_group"):
        # Open-group path: always push the observation so the arbiter has fresh
        # context, even if this bot is currently busy generating a previous
        # reply (phase=processing). The subscriber + watchdog will
        # decide who speaks for the next round.
        gid = str(buf.get("group_id") or sid.removeprefix("group_"))
        msg_id = str(buf.get("last_message_id") or "") or f"local:{int(time.time_ns())}"
        observe_payload = _build_observe_payload(buf, bot=bot, text=text or "", message_id=msg_id)
        already_subscribed = (
            gid in state.arbiter_subscriber_tasks
            and not state.arbiter_subscriber_tasks[gid].done()
        )
        if already_subscribed:
            asyncio.create_task(_post_observe_async(observe_payload))
        else:
            response = await _post_observe_async(observe_payload)
            initial_since = (
                int(response.get("latest_decision_id") or 0)
                if isinstance(response, dict)
                else 0
            )
            _ensure_arbiter_subscriber(
                group_id=gid, sid=sid, bot=bot, initial_since=initial_since
            )
        return

    if phase == "processing":
        return

    if sid in state.debounce_tasks:
        state.debounce_tasks[sid].cancel()
    state.debounce_tasks[sid] = asyncio.create_task(debounce_flush(sid))


async def debounce_flush(sid: str):
    """Local debounce flush for private / owner sessions only.

    Open-group sessions are now driven by ``arbiter_decision_subscriber`` and
    never reach this path.
    """
    try:
        wait_s = (
            load_open_group_debounce_seconds()
            if (state.msg_buffers.get(sid) or {}).get("is_open_group")
            else state.DEBOUNCE_SECONDS
        )
        await asyncio.sleep(wait_s)
    except asyncio.CancelledError:
        return

    buf = state.msg_buffers.get(sid) or {}
    is_open_group = bool(buf.get("is_open_group"))
    identity_session = _identity_session_for_context(sid, str(buf.get("identity_session") or ""))
    if is_open_group:
        # Defensive guard: should not happen now that buffer_message routes
        # open-group traffic through the arbiter. Drop the buffer rather than
        # double-act on it.
        state.msg_buffers.pop(sid, None)
        state.debounce_tasks.pop(sid, None)
        state.session_phase.pop(sid, None)
        return

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
        speaker_payload = json.dumps(buf.get("speakers") or [], ensure_ascii=False)
        reply = await asyncio.to_thread(
            chat,
            combined_text,
            sid,
            buf["is_admin"],
            image_urls or None,
            speed_hint,
            context_session=sid,
            identity_session=str(buf.get("identity_session") or sid),
            persist_user=True,
            speaker_key=speaker_payload,
            speaker_name=str(buf.get("speaker_name") or buf.get("nickname") or ""),
            speaker_qq=str(buf.get("speaker_user_id") or ""),
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
