"""Provider adapters for Anthropic and Codex CLI model calls."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


ToolHandler = Callable[[str, dict, str | None], str | list[dict]]


class ProviderError(RuntimeError):
    """Raised when a provider cannot complete the requested model call."""


def _default_codex_command() -> str:
    configured = os.environ.get("PUPU_CODEX_BIN", "").strip().strip('"')
    if configured:
        return configured

    for command in ("codex", "codex.exe"):
        resolved = shutil.which(command)
        if resolved:
            return resolved

    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidates.extend(
            [
                Path(appdata) / "npm" / "codex.cmd",
                Path(appdata) / "npm" / "codex.exe",
            ]
        )

    vscode_extensions = Path.home() / ".vscode" / "extensions"
    if vscode_extensions.exists():
        try:
            matches = sorted(
                vscode_extensions.glob(
                    "openai.chatgpt-*/bin/windows-x86_64/codex.exe"
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(matches)
        except OSError:
            pass

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return "codex"


def join_text_blocks(content_blocks) -> str:
    return "".join(block.text for block in content_blocks if block.type == "text")


def collect_reason_hint(content_blocks) -> str:
    return " ".join(
        block.text.strip()
        for block in content_blocks
        if block.type == "text" and getattr(block, "text", "").strip()
    )


@dataclass(slots=True)
class AnthropicProvider:
    client: object

    name: str = "anthropic"

    def chat_complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        **_: object,
    ) -> str:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools or None,
        )

        while response.stop_reason == "tool_use":
            if tool_handler is None:
                raise ProviderError("tool_use requested but no tool handler was supplied")

            tool_results = []
            reason_hint = collect_reason_hint(response.content)
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = tool_handler(block.name, block.input, reason_hint or None)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result if not isinstance(result, list) else result,
                    }
                )

            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools or None,
            )

        return join_text_blocks(response.content).strip()

    def json_task(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        max_tokens: int,
        **_: object,
    ) -> str:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return join_text_blocks(response.content).strip()


@dataclass(slots=True)
class CodexCliProvider:
    """Run non-interactive Codex CLI tasks using the logged-in ChatGPT account."""

    workspace_root: Path
    python_executable: str = sys.executable
    codex_command: str = field(default_factory=_default_codex_command)
    _checked_available: bool = False

    name: str = "codex_cli"

    def chat_complete(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        session_id: str = "default",
        image_urls: list[str] | None = None,
        is_admin: bool = False,
        tool_exposure: str = "chat",
        tool_handler: ToolHandler | None = None,
        **_: object,
    ) -> str:
        if tools and tool_handler and os.environ.get("PUPU_CODEX_TOOL_MODE", "bridge") == "bridge":
            return self._chat_with_tool_bridge(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                tools=tools,
                tool_handler=tool_handler,
                session_id=session_id,
            )

        prompt = self._build_chat_prompt(system, messages, max_tokens, tools or [])
        return self._run_codex_exec(
            prompt,
            task_name="chat",
            timeout_seconds=_env_int("PUPU_CODEX_CHAT_TIMEOUT", 180),
            use_mcp=bool(tools),
            session_id=session_id,
            image_urls=image_urls or [],
            is_admin=is_admin,
            tool_exposure=tool_exposure,
        )

    def _chat_with_tool_bridge(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict],
        tool_handler: ToolHandler,
        session_id: str,
    ) -> str:
        tool_names = {str(tool.get("name", "")) for tool in tools}
        tool_results: list[dict[str, str]] = []
        rounds = _env_int("PUPU_CODEX_TOOL_ROUNDS", 4)

        for round_index in range(max(1, rounds)):
            prompt = self._build_tool_bridge_prompt(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                tools=tools,
                tool_results=tool_results,
            )
            raw = self._run_codex_exec(
                prompt,
                task_name=f"chat_tool_bridge_round_{round_index + 1}",
                timeout_seconds=_env_int("PUPU_CODEX_CHAT_TIMEOUT", 180),
                use_mcp=False,
                session_id=session_id,
                image_urls=[],
                is_admin=False,
                tool_exposure="chat",
            )
            parsed = _parse_json_object(raw)
            if not parsed:
                return raw.strip()

            if parsed.get("type") == "final":
                return str(parsed.get("text", "")).strip()

            if parsed.get("type") != "tool_call":
                return str(parsed.get("text") or raw).strip()

            name = str(parsed.get("name", "")).strip()
            if name not in tool_names:
                tool_results.append(
                    {
                        "name": name,
                        "input": json.dumps(parsed.get("input", {}), ensure_ascii=False),
                        "result": f"工具不可用：{name}",
                    }
                )
                continue

            tool_input = parsed.get("input", {})
            if not isinstance(tool_input, dict):
                tool_input = {}
            result = tool_handler(name, tool_input, str(parsed.get("reason", "")).strip())
            tool_results.append(
                {
                    "name": name,
                    "input": json.dumps(tool_input, ensure_ascii=False),
                    "result": _truncate_tool_result(result),
                }
            )

        return "我刚刚试着调用工具，但是还没拿到稳定结果。你再说具体一点，我继续帮你看。"

    def _build_tool_bridge_prompt(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict],
        tool_results: list[dict[str, str]],
    ) -> str:
        transcript = []
        for message in messages:
            role = "用户" if message.get("role") == "user" else "仆仆"
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            transcript.append(f"{role}: {content}")

        tool_lines = []
        for tool in tools:
            tool_lines.append(
                json.dumps(
                    {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("input_schema", {}),
                    },
                    ensure_ascii=False,
                )
            )
        result_lines = []
        for item in tool_results:
            result_lines.append(
                f"- {item['name']} input={item['input']} result={item['result']}"
            )

        return (
            f"{system.strip()}\n\n"
            "## 当前对话\n"
            + "\n".join(transcript)
            + "\n\n## 可用工具\n"
            + "\n".join(tool_lines)
            + "\n\n## 已获得的工具结果\n"
            + ("\n".join(result_lines) if result_lines else "无")
            + "\n\n## 输出协议\n"
            "你必须只输出一个 JSON 对象，不要 markdown。\n"
            "如果需要工具，输出："
            '{"type":"tool_call","name":"工具名","input":{...},"reason":"为什么用"}\n'
            "如果可以回复用户，输出："
            '{"type":"final","text":"要发给用户的话"}\n'
            f"最终回复长度上限约 {max_tokens} tokens。"
        )

    def json_task(
        self,
        *,
        system: str,
        user_content: str,
        max_tokens: int,
        task_name: str = "json_task",
        **_: object,
    ) -> str:
        prompt = (
            f"{system.strip()}\n\n"
            "下面是任务输入。只输出最终 JSON 文本，不要 markdown，不要解释。\n\n"
            f"{user_content}"
        )
        return self._run_codex_exec(
            prompt,
            task_name=task_name,
            timeout_seconds=_env_int("PUPU_CODEX_JSON_TIMEOUT", 240),
            use_mcp=False,
            session_id="default",
            image_urls=[],
            is_admin=False,
            tool_exposure="chat",
        )

    def check_available(self) -> None:
        if self._checked_available:
            return
        try:
            version = subprocess.run(
                [self.codex_command, "--version"],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ProviderError(_codex_command_not_found(self.codex_command)) from exc
        if version.returncode != 0:
            raise ProviderError(_process_error("codex --version", version))

        try:
            status = subprocess.run(
                [self.codex_command, "login", "status"],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ProviderError(_codex_command_not_found(self.codex_command)) from exc
        status_text = f"{status.stdout}\n{status.stderr}"
        if status.returncode != 0 or "Logged in" not in status_text:
            raise ProviderError(_process_error("codex login status", status))
        self._checked_available = True

    def _build_chat_prompt(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict],
    ) -> str:
        transcript = []
        for message in messages:
            role = "用户" if message.get("role") == "user" else "仆仆"
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            transcript.append(f"{role}: {content}")

        tool_lines = []
        for tool in tools[:20]:
            name = str(tool.get("name", "")).strip()
            description = str(tool.get("description", "")).strip()
            if name:
                tool_lines.append(f"- {name}: {description[:160]}")
        tool_section = ""
        if tool_lines:
            tool_section = (
                "\n\n## PuPu MCP tools\n"
                "这些工具由本地 PuPu MCP server 提供。需要工具时，先通过 Codex 的工具发现/搜索能力找到 PuPu MCP 工具，再调用对应工具；不要假装已经调用。\n"
                + "\n".join(tool_lines)
            )

        return (
            f"{system.strip()}\n\n"
            "## 当前对话\n"
            + "\n".join(transcript)
            + tool_section
            + "\n\n## 回复要求\n"
            "- 直接输出仆仆接下来要发给用户的话。\n"
            "- 不要解释你的推理，不要输出工具调用过程，不要加前缀。\n"
            "- 如果需要搜索、定时任务、文件或系统能力，优先使用已接入的 PuPu MCP 工具。\n"
            f"- 回复长度上限约 {max_tokens} tokens。"
        )

    def _run_codex_exec(
        self,
        prompt: str,
        *,
        task_name: str,
        timeout_seconds: int,
        use_mcp: bool,
        session_id: str,
        image_urls: list[str],
        is_admin: bool,
        tool_exposure: str,
    ) -> str:
        self.check_available()

        with tempfile.TemporaryDirectory(prefix="pupu-codex-run-") as run_dir:
            output_path = Path(run_dir) / "last-message.txt"
            command = self.build_exec_command(
                output_path=output_path,
                run_dir=Path(run_dir),
                use_mcp=use_mcp,
                session_id=session_id,
                image_urls=image_urls,
                is_admin=is_admin,
                tool_exposure=tool_exposure,
            )
            print(
                f"[pupu][llm] provider=codex_cli task={task_name} "
                f"mcp={use_mcp} timeout={timeout_seconds}s"
            )
            try:
                proc = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=timeout_seconds,
                    cwd=str(self.workspace_root),
                )
            except FileNotFoundError as exc:
                raise ProviderError(_codex_command_not_found(self.codex_command)) from exc
            if proc.returncode != 0:
                raise ProviderError(_process_error("codex exec", proc))
            if not output_path.exists():
                raise ProviderError("codex exec did not write an output-last-message file")
            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                raise ProviderError("codex exec returned an empty final message")
            return text

    def build_exec_command(
        self,
        *,
        output_path: Path,
        run_dir: Path,
        use_mcp: bool,
        session_id: str,
        image_urls: list[str],
        is_admin: bool,
        tool_exposure: str,
    ) -> list[str]:
        command = [
            self.codex_command,
            "exec",
            "--json",
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "-C",
            str(run_dir),
            "-s",
            os.environ.get("PUPU_CODEX_SANDBOX", "read-only"),
        ]

        profile = os.environ.get("PUPU_CODEX_PROFILE", "").strip()
        if profile:
            command.extend(["-p", profile])

        model = os.environ.get("PUPU_CODEX_MODEL", "").strip()
        if model:
            command.extend(["-m", model])

        reasoning_effort = os.environ.get("PUPU_CODEX_REASONING_EFFORT", "").strip()
        if reasoning_effort:
            command.extend(
                [
                    "-c",
                    f"model_reasoning_effort={_toml_string(reasoning_effort)}",
                ]
            )

        if use_mcp:
            command.extend(self._mcp_config_args(session_id, image_urls, is_admin, tool_exposure))

        command.append("-")
        return command

    def _mcp_config_args(
        self,
        session_id: str,
        image_urls: list[str],
        is_admin: bool,
        tool_exposure: str,
    ) -> list[str]:
        env = {
            "PUPU_MCP_SESSION_ID": session_id,
            "PUPU_MCP_IS_ADMIN": "1" if is_admin else "0",
            "PUPU_MCP_IMAGE_URLS": json.dumps(image_urls, ensure_ascii=False),
            "PUPU_MCP_EXPOSURE": tool_exposure,
            "PYTHONPATH": str(self.workspace_root),
        }
        args = [
            "-c",
            f"mcp_servers.pupu.command={_toml_string(self.python_executable)}",
            "-c",
            'mcp_servers.pupu.args=["-m","pupu.codex_mcp_server"]',
        ]
        for key, value in env.items():
            args.extend(["-c", f"mcp_servers.pupu.env.{key}={_toml_string(value)}"])
        return args


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _toml_string(value: str) -> str:
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    return json.dumps(value)


def _parse_json_object(raw_text: str) -> dict | None:
    raw = (raw_text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        raw = "\n".join(lines)
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

    candidates = [raw]
    brace = raw.find("{")
    if brace != -1:
        candidates.append(raw[brace:])

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for variant in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                parsed, _ = decoder.raw_decode(variant)
            except Exception:
                continue
            return parsed if isinstance(parsed, dict) else None
    return None


def _truncate_tool_result(result, limit: int = 4000) -> str:
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False)
    text = text.replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _process_error(label: str, proc: subprocess.CompletedProcess) -> str:
    stdout = (proc.stdout or "").strip().replace("\n", " ")[:500]
    stderr = (proc.stderr or "").strip().replace("\n", " ")[:500]
    return f"{label} failed with code {proc.returncode}; stdout={stdout}; stderr={stderr}"


def _codex_command_not_found(command: str) -> str:
    return (
        f"codex executable not found: {command!r}. "
        "Set PUPU_CODEX_BIN to the absolute path of codex.exe, "
        "or add Codex CLI to PATH before starting PuPu."
    )
