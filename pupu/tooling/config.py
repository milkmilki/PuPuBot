"""Tool server config helpers.

Today this mostly toggles builtin servers on and off. The shape is intentionally
compatible with a future move to real MCP/external server entries in config.
"""

from __future__ import annotations

import json

from pupu.app_config import load_app_config
from pupu.config import get_config_path

DEFAULT_BUILTIN_SERVER_STATE = {
    "web": True,
    "filesystem": True,
    "system": True,
    "media": True,
    "scheduler": True,
}


def load_builtin_server_state() -> dict[str, bool]:
    state = dict(DEFAULT_BUILTIN_SERVER_STATE)
    config_path = get_config_path()
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}

        tool_servers = raw.get("tool_servers")
        if isinstance(tool_servers, dict):
            _apply_tool_server_state(state, tool_servers)
        elif isinstance(tool_servers, list):
            for item in tool_servers:
                if not isinstance(item, dict):
                    continue
                if item.get("provider", "builtin") != "builtin":
                    continue
                name = item.get("name")
                if name in state and "enabled" in item:
                    state[name] = bool(item["enabled"])

    try:
        app_cfg = load_app_config()
    except Exception:
        app_cfg = {}
    if isinstance(app_cfg.get("tool_servers"), dict):
        _apply_tool_server_state(state, app_cfg["tool_servers"])
    return state


def _apply_tool_server_state(state: dict[str, bool], tool_servers: dict) -> None:
    for name in state:
        item = tool_servers.get(name)
        if isinstance(item, bool):
            state[name] = item
        elif isinstance(item, dict) and "enabled" in item:
            state[name] = bool(item["enabled"])
