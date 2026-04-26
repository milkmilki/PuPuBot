"""Shared helpers for commands, buffering, and adapter handlers."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime

from nonebot.adapters import Event

from pupu.config import load_owner_id_set
from pupu.memory import get_last_user_message_time

from . import state


def is_owner(user_id) -> bool:
    return str(user_id) in load_owner_id_set()


def is_admin(user_id) -> bool:
    return is_owner(user_id)


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
        if index == 0 and prefix is not None:
            await bot.send(event, prefix + segment)
        else:
            await bot.send(event, segment)
        if index < len(segments) - 1:
            typing_time = min(
                4,
                max(0.8, len(segments[index + 1]) * random.uniform(0.05, 0.15)),
            )
            await asyncio.sleep(typing_time)


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
    try:
        user_id = event.get_user_id()
        if is_owner(user_id):
            return state.OWNER_SESSION
        return f"session_{event.get_session_id()}"
    except Exception:
        return "default"
