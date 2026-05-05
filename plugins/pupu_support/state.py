"""Shared runtime state for the NoneBot plugin."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from pupu.sessions import OWNER_SESSION


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}

DEBOUNCE_SECONDS = 20.0

proactive_task: asyncio.Task | None = None
scheduler_task: asyncio.Task | None = None
maintenance_task: asyncio.Task | None = None
tts_reply_enabled: bool = _env_bool("PUPU_TTS_REPLY_DEFAULT", False)

msg_buffers: dict[str, dict] = {}
debounce_tasks: dict[str, asyncio.Task] = {}
session_phase: dict[str, str] = {}

# arbiter_decision_subscriber tasks, keyed by group_id (one per open group).
arbiter_subscriber_tasks: dict[str, asyncio.Task] = {}
# Last seen ``decision_id`` per group, so the subscriber resumes from the
# correct cursor across reconnects.
arbiter_last_decision_id: dict[str, int] = {}
