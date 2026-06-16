"""System command tool server."""

from __future__ import annotations

import os
import subprocess

from ..base import BuiltinToolServer, ToolContext, ToolSpec


def run_command(command: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.expanduser("~"),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" if output else "") + result.stderr
        if not output:
            output = f"（命令执行完毕，退出码 {result.returncode}，无输出）"
        elif result.returncode != 0:
            output += f"\n（退出码 {result.returncode}）"
        if len(output) > 8000:
            output = output[:8000] + "\n\n...(输出太长，已截断)"
        return output
    except subprocess.TimeoutExpired:
        return f"命令超时（{timeout}秒）"
    except Exception as exc:
        return f"执行命令出错：{exc}"


def _handle_run_command(tool_input: dict, _context: ToolContext) -> str:
    return run_command(tool_input["command"], tool_input.get("timeout", 30))


SYSTEM_SERVER = BuiltinToolServer(
    name="system",
    description="Command execution tools.",
    tools=(
        ToolSpec(
            server="system",
            name="run_command",
            description="Run a shell command on the computer and return stdout/stderr. Use for system management, git, pip, and diagnostics. Be careful with destructive commands.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30)",
                    },
                },
                "required": ["command"],
            },
            handler=_handle_run_command,
            admin_only=True,
            legacy_names=("run_command",),
        ),
    ),
)
