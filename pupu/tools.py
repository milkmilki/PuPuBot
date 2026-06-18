"""Compatibility facade for the MCP-style tooling registry."""

from __future__ import annotations

import json
import os
import sys

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

TOOL_DEFINITIONS = get_tool_definitions("chat")
PROACTIVE_TOOL_DEFINITIONS = get_tool_definitions("proactive")


def refresh_tool_definitions() -> None:
    global TOOL_DEFINITIONS, PROACTIVE_TOOL_DEFINITIONS
    refresh_registry()
    TOOL_DEFINITIONS = get_tool_definitions("chat")
    PROACTIVE_TOOL_DEFINITIONS = get_tool_definitions("proactive")


def get_chat_tool_definitions() -> list[dict]:
    return get_tool_definitions("chat")


def get_proactive_tool_definitions() -> list[dict]:
    return get_tool_definitions("proactive")


def _truncate_text(text: str, limit: int = 240) -> str:
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _safe_log_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return str(text).encode(encoding, errors="replace").decode(encoding, errors="replace")


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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _should_log_tool_reason() -> bool:
    return _truthy(os.environ.get("PUPU_DEBUG_TOOL_REASON"))


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
        if reason_hint and _should_log_tool_reason():
            print(
                f"[pupu][tool] session={session_id} call={spec.qualified_name} "
                f"reason={_safe_log_text(_truncate_text(reason_hint, 160))} "
                f"input={_safe_log_text(input_preview)}"
            )
        else:
            print(
                f"[pupu][tool] session={session_id} call={spec.qualified_name} "
                f"input={_safe_log_text(input_preview)}"
            )

        result = execute_registered_tool(
            tool_name,
            tool_input,
            session_id=session_id,
            image_urls=image_urls,
        )
        print(
            f"[pupu][tool] session={session_id} done={spec.qualified_name} "
            f"result={_safe_log_text(_preview_tool_result(result))}"
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
    "get_chat_tool_definitions",
    "get_proactive_tool_definitions",
    "get_registry",
    "is_admin_tool",
    "list_dir",
    "look_at_image",
    "manage_scheduled_task",
    "read_file",
    "refresh_registry",
    "run_command",
    "write_file",
]
