"""Compatibility facade for the MCP-style tooling registry."""

from __future__ import annotations

import json

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


def _truncate_text(text: str, limit: int = 240) -> str:
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _preview_tool_input(tool_input: dict) -> str:
    try:
        return _truncate_text(
            json.dumps(tool_input, ensure_ascii=False, sort_keys=True),
            300,
        )
    except Exception:
        return _truncate_text(repr(tool_input), 300)


def _preview_tool_result(result) -> str:
    if isinstance(result, list):
        block_types = []
        for block in result:
            if isinstance(block, dict):
                block_types.append(str(block.get("type", "unknown")))
            else:
                block_types.append(type(block).__name__)
        return f"content_blocks={block_types}"
    return _truncate_text(str(result), 300)


def execute_tool(
    tool_name: str,
    tool_input: dict,
    image_urls: list[str] | None = None,
    session_id: str = "default",
    reason_hint: str | None = None,
):
    try:
        spec = get_registry().resolve(tool_name)
        input_preview = _preview_tool_input(tool_input)
        if reason_hint:
            print(
                f"[pupu][tool] session={session_id} call={spec.qualified_name} "
                f"reason={_truncate_text(reason_hint, 160)} input={input_preview}"
            )
        else:
            print(
                f"[pupu][tool] session={session_id} call={spec.qualified_name} "
                f"input={input_preview}"
            )

        result = execute_registered_tool(
            tool_name,
            tool_input,
            session_id=session_id,
            image_urls=image_urls,
        )
        print(
            f"[pupu][tool] session={session_id} done={spec.qualified_name} "
            f"result={_preview_tool_result(result)}"
        )
        return result
    except KeyError:
        print(f"[pupu][tool] session={session_id} unknown={tool_name}")
        return f"未知工具：{tool_name}"
    except Exception as exc:
        print(f"[pupu][tool] session={session_id} failed={tool_name} error={exc}")
        raise


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
