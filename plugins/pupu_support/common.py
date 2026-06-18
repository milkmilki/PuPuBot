"""Shared helpers for commands, buffering, and adapter handlers."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime

from nonebot.adapters import Event

from pupu.config import load_owner_id_set
from pupu.memory import get_last_user_message_time
from pupu.storage.people import OWNER_PERSON_KEY, qq_person_key, qqofficial_person_key
from pupu.tts import synthesize_reply_to_file

from . import state

try:
    from nonebot.adapters.onebot.v11 import Message as OBMessage
    from nonebot.adapters.onebot.v11 import MessageSegment as OBMsgSeg

    HAS_ONEBOT_V11 = True
except ImportError:
    OBMessage = None
    OBMsgSeg = None
    HAS_ONEBOT_V11 = False


def is_owner(user_id) -> bool:
    return str(user_id) in load_owner_id_set()


def is_admin(user_id) -> bool:
    return is_owner(user_id)


def identity_session_for_user(user_id) -> str:
    return state.OWNER_SESSION if is_owner(user_id) else f"private_{user_id}"


def person_key_for_onebot_user(user_id) -> str:
    return OWNER_PERSON_KEY if is_owner(user_id) else qq_person_key(user_id)


def person_key_for_qq_official_user(user_id) -> str:
    return OWNER_PERSON_KEY if is_owner(user_id) else qqofficial_person_key(user_id)


def compute_reply_speed_hint(session_id: str) -> str | None:
    last_ts = get_last_user_message_time(session_id)
    if not last_ts:
        return None
    try:
        last_dt = datetime.fromisoformat(last_ts)
        delta = (datetime.now() - last_dt).total_seconds()
    except Exception:
        return None

    if delta < 10:
        return "用户刚刚秒回了你，回复非常快"
    if delta < 120:
        return "用户回复速度正常"
    if delta < 600:
        return "用户隔了好几分钟才回复，可能在忙别的"
    return "用户隔了很久才回复，可能刚回来"


def split_message(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return [text]
    parts = [line.strip() for line in text.split("\n") if line.strip()]
    return parts if parts else [text]


async def send_segments(bot, event, segments: list[str], prefix=None):
    for index, segment in enumerate(segments):
        message = _build_outgoing_message(bot, segment, prefix if index == 0 else None)
        if index == 0 and prefix is not None:
            await bot.send(event, message)
        else:
            await bot.send(event, message)
        if index < len(segments) - 1:
            typing_time = min(
                4,
                max(0.8, len(segments[index + 1]) * random.uniform(0.05, 0.15)),
            )
            await asyncio.sleep(typing_time)
    await maybe_send_voice_reply(bot, event, "\n".join(segments))


_AT_PROTOCOL_RE = re.compile(r"<at\s+qq=[\"']?(\d+)[\"']?\s*/>")


def _build_outgoing_message(bot, text: str, prefix=None):
    if not _is_onebot_v11_bot(bot) or OBMessage is None or OBMsgSeg is None:
        return (prefix + text) if prefix is not None else text
    if not _AT_PROTOCOL_RE.search(text or ""):
        return (prefix + text) if prefix is not None else text
    message = OBMessage()
    if prefix is not None:
        message += prefix
    pos = 0
    for match in _AT_PROTOCOL_RE.finditer(text):
        before = text[pos:match.start()]
        if before:
            message += OBMsgSeg.text(before)
        message += OBMsgSeg.at(int(match.group(1)))
        pos = match.end()
    rest = text[pos:]
    if rest:
        message += OBMsgSeg.text(rest)
    return message


def _is_onebot_v11_bot(bot) -> bool:
    if not HAS_ONEBOT_V11:
        return False
    module = getattr(bot.__class__, "__module__", "")
    return module.startswith("nonebot.adapters.onebot.v11")


async def maybe_send_voice_reply(bot, event, text: str) -> None:
    if not state.tts_reply_enabled:
        return
    if not _is_onebot_v11_bot(bot):
        return
    try:
        audio_path = await asyncio.to_thread(synthesize_reply_to_file, text)
        if not audio_path:
            return
        await bot.send(event, OBMsgSeg.record(audio_path))
    except Exception as exc:
        print(f"[pupu][tts] send failed: {exc}")


async def send_private_segments(bot, user_id: int, segments: list[str]):
    for index, segment in enumerate(segments):
        await bot.send_private_msg(user_id=user_id, message=segment)
        if index < len(segments) - 1:
            typing_time = min(
                4,
                max(0.8, len(segments[index + 1]) * random.uniform(0.05, 0.15)),
            )
            await asyncio.sleep(typing_time)


def log(direction: str, session: str, user: str, text: str):
    now = datetime.now().strftime("%H:%M:%S")
    arrow = "<<<" if direction == "recv" else ">>>"
    label = "收到" if direction == "recv" else "发送"
    display = text[:120] + "..." if len(text) > 120 else text
    print(f"[{now}] {arrow} {label} | {session} | {user} | {display}")


def resolve_session(event: Event) -> str:
    context_session, _identity_session = resolve_sessions(event)
    return context_session


def resolve_sessions(event: Event) -> tuple[str, str]:
    """Return (context_session, identity_session) for a QQ event.

    Context tracks where a conversation happened; identity tracks who the user is.
    In private chats they are the same. In groups, the context is the group while
    the identity is the sender (or owner).
    """
    try:
        user_id = event.get_user_id()
        identity_session = identity_session_for_user(user_id)
        group_id = getattr(event, "group_id", None)
        if group_id is not None:
            return f"group_{group_id}", identity_session
        return identity_session, identity_session
    except Exception:
        return "default", "default"
