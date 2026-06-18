"""QQ official adapter handlers."""

from __future__ import annotations

from nonebot import on_message

from pupu.config import is_private_reply_allowed
from pupu.richmsg import parse_qq_official_message

from . import state
from .buffering import buffer_message
from .common import is_admin, person_key_for_qq_official_user

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
        identity_sid = state.OWNER_SESSION if is_admin(user) else f"c2c_{user}"
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(user),
            user,
            f"频道{event.channel_id}",
            identity_session=identity_sid,
            speaker_key=person_key_for_qq_official_user(user),
            speaker_user_id=str(user),
            speaker_name=str(user),
        )

    @qq_c2c.handle()
    async def handle_qq_c2c(bot: QQBot, event: C2CMessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        user = event.get_user_id()
        if not is_private_reply_allowed(user):
            print(f"[pupu] private message ignored by whitelist: user_id={user}")
            return
        text, image_urls = parse_qq_official_message(
            event.get_message(),
            getattr(event, "attachments", None),
        )
        if not text and not image_urls:
            return
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
            identity_session=sid,
            speaker_key=person_key_for_qq_official_user(user),
            speaker_user_id=str(user),
            speaker_name=str(user),
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
        identity_sid = state.OWNER_SESSION if is_admin(user) else f"c2c_{user}"
        await buffer_message(
            sid,
            text,
            image_urls,
            bot,
            event,
            is_admin(user),
            user,
            "QQ群",
            identity_session=identity_sid,
            speaker_key=person_key_for_qq_official_user(user),
            speaker_user_id=str(user),
            speaker_name=str(user),
        )
