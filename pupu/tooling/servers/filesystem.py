"""Filesystem tool server."""

from __future__ import annotations

from pathlib import Path

from ..base import BuiltinToolServer, ToolContext, ToolSpec


def read_file(path: str, max_lines: int = 200) -> str:
    try:
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            return f"文件不存在：{file_path}"
        if not file_path.is_file():
            return f"不是文件：{file_path}"
        if file_path.stat().st_size > 5 * 1024 * 1024:
            size_mb = file_path.stat().st_size // 1024 // 1024
            return f"文件太大（{size_mb}MB），跳过"
        text = file_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n\n...(共 {len(lines)} 行，只显示前 {max_lines} 行)"
        return text
    except Exception as exc:
        return f"读取文件出错：{exc}"


def write_file(path: str, content: str) -> str:
    try:
        file_path = Path(path).expanduser().resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"已写入 {file_path}（{len(content)} 字符）"
    except Exception as exc:
        return f"写入文件出错：{exc}"


def list_dir(path: str = ".") -> str:
    try:
        dir_path = Path(path).expanduser().resolve()
        if not dir_path.exists():
            return f"路径不存在：{dir_path}"
        if not dir_path.is_dir():
            return f"不是目录：{dir_path}"
        entries = sorted(dir_path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        if not entries:
            return f"{dir_path} 是空目录"

        lines = [f"[DIR] {dir_path}\n"]
        for entry in entries[:100]:
            if entry.is_dir():
                lines.append(f"  [DIR]  {entry.name}/")
                continue

            size = entry.stat().st_size
            if size < 1024:
                size_str = f"{size}B"
            elif size < 1024 * 1024:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // 1024 // 1024}MB"
            lines.append(f"  {size_str:>6}  {entry.name}")

        if len(entries) > 100:
            lines.append(f"\n...(共 {len(entries)} 项，只显示前 100 项)")
        return "\n".join(lines)
    except Exception as exc:
        return f"列出目录出错：{exc}"


def _handle_read_file(tool_input: dict, _context: ToolContext) -> str:
    return read_file(tool_input["path"], tool_input.get("max_lines", 200))


def _handle_write_file(tool_input: dict, _context: ToolContext) -> str:
    return write_file(tool_input["path"], tool_input["content"])


def _handle_list_dir(tool_input: dict, _context: ToolContext) -> str:
    return list_dir(tool_input.get("path", "."))


FILESYSTEM_SERVER = BuiltinToolServer(
    name="filesystem",
    description="Filesystem read/write tools.",
    tools=(
        ToolSpec(
            server="filesystem",
            name="read_file",
            description="Read the contents of a file on the computer. Use this to inspect configs, logs, code, or any text file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max lines to read (default 200, prevents huge output)",
                    },
                },
                "required": ["path"],
            },
            handler=_handle_read_file,
            admin_only=True,
            legacy_names=("read_file",),
        ),
        ToolSpec(
            server="filesystem",
            name="write_file",
            description="Write content to a file. Creates the file if it does not exist and overwrites it if it does.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write",
                    },
                },
                "required": ["path", "content"],
            },
            handler=_handle_write_file,
            admin_only=True,
            legacy_names=("write_file",),
        ),
        ToolSpec(
            server="filesystem",
            name="list_dir",
            description="List files and directories in a given path. Shows file sizes and types.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: current directory)",
                    }
                },
                "required": [],
            },
            handler=_handle_list_dir,
            admin_only=True,
            legacy_names=("list_dir",),
        ),
    ),
)
