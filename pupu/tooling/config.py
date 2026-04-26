"""Tool server config helpers.

Today this mostly toggles builtin servers on and off. The shape is intentionally
compatible with a future move to real MCP/external server entries in config.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "config.json"

DEFAULT_BUILTIN_SERVER_STATE = {
    "web": True,
    "filesystem": True,
    "system": True,
    "media": True,
    "scheduler": True,
}


def load_builtin_server_state() -> dict[str, bool]:
    state = dict(DEFAULT_BUILTIN_SERVER_STATE)
    if not CONFIG_PATH.exists():
        return state

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return state

    tool_servers = raw.get("tool_servers")
    if isinstance(tool_servers, dict):
        for name in state:
            item = tool_servers.get(name)
            if isinstance(item, bool):
                state[name] = item
            elif isinstance(item, dict) and "enabled" in item:
                state[name] = bool(item["enabled"])
        return state

    if isinstance(tool_servers, list):
        for item in tool_servers:
            if not isinstance(item, dict):
                continue
            if item.get("provider", "builtin") != "builtin":
                continue
            name = item.get("name")
            if name in state and "enabled" in item:
                state[name] = bool(item["enabled"])
    return state
