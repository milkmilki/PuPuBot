"""Core MCP-style tool abstractions used by builtin and future external servers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..model_runtime import ToolResult

ToolResultValue = str | list[dict[str, Any]] | ToolResult
ToolHandler = Callable[[dict[str, Any], "ToolContext"], ToolResultValue]


@dataclass(slots=True, frozen=True)
class ToolContext:
    """Runtime information shared with tool handlers."""

    session_id: str = "default"
    image_urls: list[str] | None = None


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """A single tool exposed through an MCP-style server namespace."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    admin_only: bool = False
    exposures: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))
    legacy_names: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None

    @property
    def qualified_name(self) -> str:
        return f"mcp__{self.server}__{self.name}"

    def to_api_definition(self) -> dict[str, Any]:
        return {
            "name": self.qualified_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class BaseToolServer(ABC):
    """Abstract tool server boundary.

    Builtin tools and future MCP-backed transports share this contract so the
    registry does not care whether a tool is local or remote.
    """

    def __init__(self, name: str, description: str, provider: str = "builtin") -> None:
        self.name = name
        self.description = description
        self.provider = provider

    @abstractmethod
    def list_tools(self) -> tuple[ToolSpec, ...]:
        """Return the tools exposed by this server."""

    def tools_changed(self) -> bool:
        """Return whether the server's tool catalog needs to be reloaded."""

        return False


class BuiltinToolServer(BaseToolServer):
    """Simple in-process tool server implementation."""

    def __init__(
        self,
        name: str,
        description: str,
        tools: tuple[ToolSpec, ...],
        provider: str = "builtin",
    ) -> None:
        super().__init__(name=name, description=description, provider=provider)
        self._tools = tools

    def list_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools
