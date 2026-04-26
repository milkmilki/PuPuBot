"""QQ official adapter handlers."""

from __future__ import annotations

from nonebot import on_message

from pupu.richmsg import parse_qq_official_message

from . import state
from .buffering import buffer_message
from .common import is_admin

try:
    from nonebot.adapters.qq import (
        Bot as QQBot,
        C2CMessageCreateEvent,
        GroupAtMessageCreateEvent,
        MessageCreateEvent,
    )

    HAS_QQ_OFFICIAL = True
except ImportError:
    HAS_QQ_OFFICIAL = False


if HAS_QQ_OFFICIAL:
    qq_channel = on_message(priority=10, block=True)
    qq_c2c = on_message(priority=10, block=True)
    qq_group_at = on_message(priority=10, block=True)

    @qq_channel.handle()
    async def handle_qq_channel(bot: QQBot, event: MessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        text, image_urls = parse_qq_official_message(
            event.get_message(),
            getattr(event, "attachments", None),
        )
        if not text and not image_urls:
            return
        sid = f"channel_{event.channel_id}"
        user = event.get_user_id()
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(user),
            user,
            f"频道{event.channel_id}",
        )

    @qq_c2c.handle()
    async def handle_qq_c2c(bot: QQBot, event: C2CMessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        text, image_urls = parse_qq_official_message(
            event.get_message(),
            getattr(event, "attachments", None),
        )
        if not text and not image_urls:
            return
        user = event.get_user_id()
        sid = state.OWNER_SESSION if is_admin(user) else f"c2c_{user}"
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(user),
            user,
            "私聊",
        )

    @qq_group_at.handle()
    async def handle_qq_group_at(bot: QQBot, event: GroupAtMessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        text, image_urls = parse_qq_official_message(
            event.get_message(),
            getattr(event, "attachments", None),
        )
        if not text and not image_urls:
            return
        sid = f"qqgroup_{event.group_openid}"
        user = event.get_user_id()
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(user),
            user,
            "QQ群",
        )
