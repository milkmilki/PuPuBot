"""Public MCP-style tool registry entrypoints."""

from .base import ToolContext
from .registry import get_registry, refresh_registry


def get_tool_definitions(exposure: str = "chat") -> list[dict]:
    return get_registry().get_api_definitions(exposure=exposure)


def execute_registered_tool(
    tool_name: str,
    tool_input: dict,
    *,
    session_id: str = "default",
    image_urls: list[str] | None = None,
):
    return get_registry().execute(
        tool_name,
        tool_input,
        ToolContext(session_id=session_id, image_urls=image_urls),
    )


def is_registered_admin_tool(tool_name: str) -> bool:
    return get_registry().is_admin_tool(tool_name)
