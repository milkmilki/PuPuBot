"""Shared slash-command registry and help rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    primary: str
    aliases: tuple[str, ...] = ()
    category: str = "其他"
    description: str = ""
    surfaces: frozenset[str] = field(default_factory=lambda: frozenset({"cli", "qq"}))
    admin_surfaces: frozenset[str] = field(default_factory=frozenset)
    usage: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return (self.primary, *self.aliases)


def _surface_set(values: Iterable[str]) -> frozenset[str]:
    return frozenset(str(value).strip().lower() for value in values if str(value).strip())


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "help",
        "help",
        aliases=("commands", "帮助", "命令", "指令"),
        category="基础",
        description="查看这份帮助",
    ),
    CommandSpec(
        "quit",
        "quit",
        aliases=("exit", "q"),
        category="基础",
        description="退出 CLI",
        surfaces=_surface_set(("cli",)),
    ),
    CommandSpec("score", "score", category="基础", description="查看好感度"),
    CommandSpec("history", "history", category="基础", description="查看最近聊天记录"),
    CommandSpec(
        "tasks",
        "tasks",
        aliases=("定时任务",),
        category="基础",
        description="查看定时任务",
    ),
    CommandSpec(
        "events",
        "events",
        aliases=("事件线", "记忆事件"),
        category="记忆",
        description="查看事件线记忆",
        usage=(
            "/events\n"
            "/events detail <key>\n"
            "/events search [--debug] <内容>\n"
            "/events url"
        ),
    ),
    CommandSpec(
        "facts",
        "facts",
        aliases=("fact", "memory_facts", "长期记忆", "事实记忆"),
        category="记忆",
        description="查看长期事实记忆",
        usage="/facts\n/facts search [--debug] <内容>",
    ),
    CommandSpec(
        "recall",
        "recall",
        aliases=("memu_recall", "召回"),
        category="记忆",
        description="调试 memU 会召回哪些记忆",
        usage="/recall <内容>",
    ),
    CommandSpec(
        "tidy",
        "tidy",
        aliases=("cleanup", "整理记忆", "整理"),
        category="记忆",
        description="整理 memU 长期记忆，默认 apply，也可用 check",
        usage="/tidy [check|apply|rebuild]",
        admin_surfaces=_surface_set(("qq",)),
    ),
    CommandSpec(
        "reset",
        "reset",
        category="记忆",
        description="重置当前会话记忆、好感度和聊天记录",
        admin_surfaces=_surface_set(("qq",)),
    ),
    CommandSpec(
        "voice",
        "voice",
        aliases=("tts", "语音", "语音回复"),
        category="语音",
        description="查看、开启或关闭语音回复",
        usage="/voice [on|off]",
        surfaces=_surface_set(("qq",)),
        admin_surfaces=_surface_set(("qq",)),
    ),
    CommandSpec(
        "provider",
        "provider",
        aliases=("llm", "模型源", "模型"),
        category="模型",
        description="查看或切换模型源；role 支持 chat / judge / maintenance / proactive",
        usage="/provider [status|<provider>|<role> <provider>|all <provider>]",
        surfaces=_surface_set(("qq",)),
        admin_surfaces=_surface_set(("qq",)),
    ),
    CommandSpec(
        "proactive",
        "proactive",
        aliases=("主动", "主动消息"),
        category="主动消息",
        description="查看、开启或关闭主动消息；QQ 侧还支持 force 手动执行一次",
        usage="/proactive [status|on|off|force]",
        admin_surfaces=_surface_set(("qq",)),
    ),
    CommandSpec(
        "debug",
        "debug",
        aliases=("调试", "debug_console"),
        category="调试",
        description="查看、开启或关闭控制台调试日志",
        usage="/debug [status|on|off]",
        surfaces=_surface_set(("cli", "qq")),
        admin_surfaces=_surface_set(("qq",)),
    ),
    CommandSpec(
        "silence",
        "silence",
        aliases=("silenc", "沉默", "静默", "仲裁静默"),
        category="群仲裁",
        description="查看或切换本群仲裁静默状态，仅群聊可用",
        usage="/silence [on|off]",
        surfaces=_surface_set(("qq",)),
        admin_surfaces=_surface_set(("qq",)),
    ),
)

_BY_ID = {spec.command_id: spec for spec in COMMANDS}


def get_command(command_id: str) -> CommandSpec:
    return _BY_ID[command_id]


def command_aliases(command_id: str) -> set[str]:
    return set(get_command(command_id).aliases)


def command_usage(command_id: str) -> str:
    spec = get_command(command_id)
    return spec.usage or f"/{spec.primary}"


def _normalize_name(name: str) -> str:
    return str(name or "").strip().lstrip("/").lower()


def resolve_command(name: str, *, surface: str) -> CommandSpec | None:
    normalized = _normalize_name(name)
    surface = str(surface or "").strip().lower()
    if not normalized or not surface:
        return None
    for spec in COMMANDS:
        if surface not in spec.surfaces:
            continue
        if normalized in {_normalize_name(item) for item in spec.names}:
            return spec
    return None


def iter_commands(*, surface: str) -> tuple[CommandSpec, ...]:
    surface = str(surface or "").strip().lower()
    return tuple(spec for spec in COMMANDS if surface in spec.surfaces)


def _format_command_line(spec: CommandSpec, *, surface: str) -> str:
    names = [f"/{spec.primary}", *(f"/{alias}" for alias in spec.aliases)]
    label = names[0] if len(names) == 1 else f"{names[0]}（{' '.join(names[1:])}）"
    desc = spec.description
    if surface in spec.admin_surfaces:
        desc += "（管理员）"
    return f"{label}：{desc}"


def render_help(*, surface: str, title: str | None = None) -> str:
    surface = str(surface or "").strip().lower()
    title = title or ("PuPu CLI 可用命令" if surface == "cli" else "PuPu 可用命令")
    lines = [title]
    current_category = ""
    for spec in iter_commands(surface=surface):
        if spec.category != current_category:
            current_category = spec.category
            lines.extend(("", f"{current_category}："))
        lines.append(_format_command_line(spec, surface=surface))
        if spec.usage and spec.command_id not in {"recall"}:
            for index, usage_line in enumerate(spec.usage.splitlines()):
                prefix = "  用法：" if index == 0 else "        "
                lines.append(f"{prefix}{usage_line}")
    return "\n".join(lines).rstrip()


__all__ = [
    "COMMANDS",
    "CommandSpec",
    "command_aliases",
    "command_usage",
    "get_command",
    "iter_commands",
    "render_help",
    "resolve_command",
]
