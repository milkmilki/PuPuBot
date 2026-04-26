"""Shared runtime state for the NoneBot plugin."""

from __future__ import annotations

import asyncio

OWNER_SESSION = "owner"
DEBOUNCE_SECONDS = 20.0

proactive_task: asyncio.Task | None = None
scheduler_task: asyncio.Task | None = None
proactive_followup_task: asyncio.Task | None = None
maintenance_task: asyncio.Task | None = None

msg_buffers: dict[str, dict] = {}
debounce_tasks: dict[str, asyncio.Task] = {}
session_phase: dict[str, str] = {}
