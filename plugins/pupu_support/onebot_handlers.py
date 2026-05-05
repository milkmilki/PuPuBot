"""OneBot v11 message handlers and lifecycle hooks."""

from __future__ import annotations

import asyncio

from nonebot import get_driver, on_message
from nonebot.rule import Rule, to_me

from pupu.config import load_open_group_ids, load_owner_ids, load_peer_config
from pupu.familiarity import PROACTIVE_THRESHOLD
from pupu.memory import get_familiarity
from pupu.proactive import proactive_loop
from pupu.richmsg import parse_onebot_message
from pupu.scheduler import onebot_scheduled_tasks_loop

from . import state
from .buffering import buffer_message, register_owner_wait_followup_sender
from .common import (
    identity_session_for_user,
    is_admin,
    is_owner,
    log,
    send_private_segments,
    split_message,
)

try:
    from nonebot.adapters.onebot.v11 import (
        Bot as OBBot,
        GroupMessageEvent as OBGroupEvent,
        MessageSegment as OBMsgSeg,
        PrivateMessageEvent as OBPrivateEvent,
    )

    HAS_ONEBOT = True
except ImportError:
    HAS_ONEBOT = False


if HAS_ONEBOT:
    async def _open_group_rule(event: OBGroupEvent) -> bool:
        if not isinstance(event, OBGroupEvent):
            return False
        return str(event.group_id) in load_open_group_ids()

    ob_open_group = on_message(rule=Rule(_open_group_rule), priority=9, block=True)
    ob_private = on_message(priority=10, block=True)
    ob_group = on_message(rule=to_me(), priority=10, block=True)
    driver = get_driver()

    def _speaker_prefix(user_id, nickname: str) -> tuple[str, bool]:
        peer = load_peer_config()
        peer_qq = str(peer.get("qq") or "").strip()
        peer_name = str(peer.get("name") or "").strip()
        if peer_qq and str(user_id) == peer_qq:
            display = peer_name or nickname or str(user_id)
            return f"[bot {display}(QQ:{user_id})] ", True
        display = nickname or str(user_id)
        return f"[{display}(QQ:{user_id})] ", False

    @ob_private.handle()
    async def handle_ob_private(bot: OBBot, event: OBPrivateEvent):
        text, image_urls = parse_onebot_message(event.get_message())
        if not text and not image_urls:
            return
        sid = identity_session_for_user(event.user_id)
        nickname = event.sender.nickname or str(event.user_id)
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(event.user_id),
            nickname,
            "私聊",
            identity_session=sid,
        )

    @ob_open_group.handle()
    async def handle_ob_open_group(bot: OBBot, event: OBGroupEvent):
        text, image_urls = parse_onebot_message(event.get_message())
        if not text and not image_urls:
            return
        sid = f"group_{event.group_id}"
        identity_sid = identity_session_for_user(event.user_id)
        nickname = event.sender.nickname or str(event.user_id)
        prefix, speaker_is_bot = _speaker_prefix(event.user_id, nickname)
        display_text = f"{prefix}{text}" if text else prefix.strip()
        await buffer_message(
            sid,
            display_text,
            image_urls,
            bot,
            event,
            is_admin(event.user_id),
            nickname,
            f"群{event.group_id}",
            reply_prefix=None,
            identity_session=identity_sid,
            is_open_group=True,
            group_id=str(event.group_id),
            message_id=str(getattr(event, "message_id", "")),
            speaker_user_id=str(event.user_id),
            speaker_name=nickname,
            speaker_is_bot=speaker_is_bot,
        )

    @ob_group.handle()
    async def handle_ob_group(bot: OBBot, event: OBGroupEvent):
        if str(event.group_id) in load_open_group_ids():
            return
        text, image_urls = parse_onebot_message(event.get_message())
        if not text and not image_urls:
            return
        sid = f"group_{event.group_id}"
        identity_sid = identity_session_for_user(event.user_id)
        nickname = event.sender.nickname or str(event.user_id)
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(event.user_id),
            nickname,
            f"群{event.group_id}",
            reply_prefix=OBMsgSeg.at(event.user_id) + " ",
            identity_session=identity_sid,
        )

    @driver.on_bot_connect
    async def on_ob_connect(bot):
        if not isinstance(bot, OBBot):
            return

        try:
            info = await bot.get_login_info()
            nickname = info.get("nickname", "unknown")
            uin = info.get("user_id", bot.self_id)
            print()
            print("=" * 40)
            print("  NapCat connected!")
            print(f"  Bot QQ: {uin}")
            print(f"  Nickname: {nickname}")
            print("=" * 40)
            print()
        except Exception:
            print(f"[pupu] NapCat connected (bot: {bot.self_id})")

        if state.scheduler_task is None or state.scheduler_task.done():
            state.scheduler_task = asyncio.create_task(onebot_scheduled_tasks_loop(bot))
            print("[pupu] scheduled tasks loop started")

        try:
            register_owner_wait_followup_sender(bot, asyncio.get_running_loop())
        except Exception as exc:
            print(f"[pupu] register owner wait_followup sender failed: {exc}")

        if state.proactive_task is not None:
            return

        owner_qq = None
        for oid in load_owner_ids():
            if oid.isdigit():
                owner_qq = oid
                break

        if not owner_qq:
            print("[pupu] no numeric owner QQ found, proactive messaging disabled")
            return

        score = get_familiarity(state.OWNER_SESSION)
        if score >= PROACTIVE_THRESHOLD:

            async def send_to_owner(text: str):
                segments = split_message(text)
                await send_private_segments(bot, int(owner_qq), segments)
                log("send", "私聊", str(owner_qq), text)

            state.proactive_task = asyncio.create_task(proactive_loop(send_to_owner))
            print(f"[pupu] proactive messaging enabled (familiarity: {score})")
        else:
            print(
                f"[pupu] proactive messaging disabled "
                f"(familiarity: {score} < {PROACTIVE_THRESHOLD})"
            )

    @driver.on_bot_disconnect
    async def on_ob_disconnect(bot):
        if not isinstance(bot, OBBot):
            return
        print(f"[pupu] NapCat disconnected (bot: {bot.self_id})")
        if state.scheduler_task is not None:
            state.scheduler_task.cancel()
            state.scheduler_task = None
        if state.proactive_task is not None:
            state.proactive_task.cancel()
            state.proactive_task = None
