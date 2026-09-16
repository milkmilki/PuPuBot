"""Small transport-neutral message types used by instance actors."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ActorInboundMessage:
    session_id: str
    identity_session: str
    user_id: str
    user_name: str
    text: str
    image_urls: list[str] = field(default_factory=list)
    is_admin: bool = False
    speaker_key: str = ""
    speaker_name: str = ""
    speaker_is_bot: bool = False
    group_id: str = ""
    message_id: str = ""
    reply_at_user_id: str = ""
    surface: str = "qq"

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)


@dataclass(frozen=True, slots=True)
class ActorOutboundTarget:
    session_id: str
    user_id: str = ""
    group_id: str = ""
    reply_at_user_id: str = ""

