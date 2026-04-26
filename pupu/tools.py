"""Compatibility facade for the MCP-style tooling registry."""

from __future__ import annotations

from .tooling import (
    execute_registered_tool,
    get_registry,
    get_tool_definitions,
    is_registered_admin_tool,
    refresh_registry,
)
from .tooling.servers.filesystem import list_dir, read_file, write_file
from .tooling.servers.media import look_at_image
from .tooling.servers.scheduler import manage_scheduled_task
from .tooling.servers.system import run_command
from .tooling.servers.web import fetch_url, web_search

TOOL_DEFINITIONS = get_tool_definitions("chat")
PROACTIVE_TOOL_DEFINITIONS = get_tool_definitions("proactive")


def execute_tool(
    tool_name: str,
    tool_input: dict,
    image_urls: list[str] | None = None,
    session_id: str = "default",
):
    try:
        return execute_registered_tool(
            tool_name,
            tool_input,
            session_id=session_id,
            image_urls=image_urls,
        )
    except KeyError:
        return f"未知工具：{tool_name}"


def is_admin_tool(tool_name: str) -> bool:
    return is_registered_admin_tool(tool_name)


def describe_tool_servers() -> list[dict[str, str | int]]:
    return get_registry().describe_servers()


__all__ = [
    "PROACTIVE_TOOL_DEFINITIONS",
    "TOOL_DEFINITIONS",
    "describe_tool_servers",
    "execute_tool",
    "fetch_url",
    "get_registry",
    "is_admin_tool",
    "list_dir",
    "look_at_image",
    "manage_scheduled_task",
    "read_file",
    "refresh_registry",
    "run_command",
    "web_search",
    "write_file",
]
