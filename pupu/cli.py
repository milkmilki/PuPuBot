"""Terminal chat interface with command handling and periodic scheduler tick."""

import os
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent import chat
from .backup import maybe_run_daily_backup
from .dialogue_loop import register_sender
from .sessions import OWNER_SESSION
from .facts_report import format_facts_report
from .important_event_report import format_important_events_report
from .llm import preflight_model_providers
from .logging_utils import setup_runtime_logging
from .memory import get_familiarity_info, get_recent_messages, init_db, reset_session
from .memory_index import (
    clear_memu_session,
    format_memu_recall_report,
    rebuild_memu_session,
    run_memu_maintenance,
)
from .proactive_control import is_proactive_enabled, set_proactive_enabled
from .tools import manage_scheduled_task

console = Console()

TIDY_USAGE = "用法：/tidy [check|apply]"
DAILY_BACKUP_CHECK_INTERVAL_SECONDS = 30 * 60

CLI_HELP_TEXT = """PuPu CLI 可用命令

基础：
/help（/commands /帮助 /命令 /指令）：查看这份帮助
/quit（/exit /q）：退出
/score：查看好感度
/history：查看最近聊天记录
/tasks（/定时任务）：查看定时任务

记忆：
/important（/events /important_events /重要事件 /记忆事件）：查看事件线记忆；支持 detail <key> / search <内容> / url / migrate [simple]
/facts（/fact /memory_facts /长期记忆 /事实记忆）：查看长期事实记忆
/recall <内容>（/memu_recall /召回）：调试 memU 会召回哪些记忆
/memu_rebuild（/rebuild_memory /重建记忆）：从旧库重建当前会话的 memU 索引
/tidy（/cleanup /整理记忆 /整理）：整理 memU 长期记忆（facts / important_events），默认执行 apply，也可用 /tidy check
/proactive [status|on|off]：查看、开启或关闭主动消息开关
/reset：重置当前会话记忆、好感度和聊天记录
"""


def _cli_scheduler_loop():
    from pupu.scheduler import cli_scheduled_tasks_tick

    last_backup_check = 0.0
    while True:
        time.sleep(45)
        try:
            cli_scheduled_tasks_tick()
            now = time.monotonic()
            if now - last_backup_check >= DAILY_BACKUP_CHECK_INTERVAL_SECONDS:
                last_backup_check = now
                backup_report = maybe_run_daily_backup()
                if backup_report:
                    print(f"[pupu] auto backup\n{backup_report}")
        except Exception as e:
            print(f"[pupu] cli scheduler: {e}")


def print_banner():
    score_info = get_familiarity_info(OWNER_SESSION)
    console.print(
        Panel(
            f"[bold]仆仆[/bold] — 好感度: Lv.{score_info['level']}\n"
            f"输入消息开始聊天 | /help 命令 | /quit 退出 | /score 好感度 | /history 最近聊天 | /tasks 定时任务 | /important 重要事件 | /facts 长期 facts | /tidy 整理 memU 记忆",
            style="cyan",
        )
    )


def _parse_tidy_mode(command_arg: str) -> tuple[str | None, str | None]:
    mode = command_arg.strip().lower()
    if not mode:
        return "apply", None
    if mode in {"check", "apply"}:
        return mode, None
    return None, TIDY_USAGE


def _apply_instance_env(instance_dir: Path) -> None:
    inst = instance_dir.resolve()
    os.environ["PUPU_INSTANCE_DIR"] = str(inst)
    os.environ["PUPU_CONFIG_PATH"] = str(inst / "instance.json")
    os.environ["PUPU_DB_PATH"] = str(inst / "data" / "pupu.db")
    os.environ["PUPU_MEMU_DB_PATH"] = str(inst / "data" / "memu.db")
    os.environ["PUPU_PERSONA_PATH"] = str(inst / "persona.json")
    (inst / "data").mkdir(parents=True, exist_ok=True)
    (inst / "data" / "logs").mkdir(parents=True, exist_ok=True)

    env_file = inst / ".env.qq"
    if env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)


def _configure_cli_instance_interactively() -> str | None:
    """Let direct CLI launches choose a managed instance.

    ``pupu.instance_main --dir`` sets ``PUPU_INSTANCE_DIR`` before importing the
    CLI, so that path stays non-interactive and already points at one instance.
    """
    if os.environ.get("PUPU_INSTANCE_DIR"):
        return None

    try:
        from pupu_console import instance_store
    except Exception:
        return None

    try:
        instance_ids = instance_store.list_instance_ids()
    except Exception:
        return None
    if not instance_ids:
        return None

    choices = []
    for instance_id in instance_ids:
        try:
            cfg, _persona = instance_store.read_instance_files(instance_id)
            inst_dir = instance_store.instance_dir(instance_id).resolve()
            choices.append(
                {
                    "id": instance_id,
                    "display_name": str(cfg.get("display_name") or instance_id),
                    "qq_mode": str(cfg.get("qq_mode") or "napcat"),
                    "port": instance_store.read_port(inst_dir),
                    "dir": inst_dir,
                    "db_exists": (inst_dir / "data" / "pupu.db").is_file(),
                }
            )
        except Exception:
            continue
    if not choices:
        return None

    console.print("[bold cyan]选择要进入的 PuPu 实例[/bold cyan]")
    console.print("[dim]直接回车使用根目录默认 CLI；输入编号或实例 id 进入对应实例。[/dim]")
    for index, choice in enumerate(choices, start=1):
        db_hint = "有记忆库" if choice["db_exists"] else "暂无记忆库"
        console.print(
            f"{index}. {choice['display_name']} "
            f"[dim]id={choice['id']} mode={choice['qq_mode']} port={choice['port']} {db_hint}[/dim]"
        )

    by_id = {str(choice["id"]): choice for choice in choices}
    while True:
        raw = console.input("[bold green]实例> [/bold green]").strip()
        if not raw:
            console.print("[dim]使用根目录默认 CLI。[/dim]")
            return None
        if raw.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)
        selected = None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices):
                selected = choices[idx - 1]
        if selected is None:
            selected = by_id.get(raw)
        if selected is None:
            console.print("[yellow]没有这个实例，请输入列表里的编号或 id；直接回车使用默认 CLI。[/yellow]")
            continue

        _apply_instance_env(Path(selected["dir"]))
        console.print(
            f"[green]已选择实例：{selected['display_name']} "
            f"({selected['id']})[/green]"
        )
        return str(selected["id"])


def handle_command(cmd: str) -> bool:
    """Handle slash commands. Returns True if handled."""
    command_name, _, command_arg = cmd.partition(" ")
    if command_name in ("/help", "/commands", "/帮助", "/命令", "/指令"):
        console.print(CLI_HELP_TEXT)
        return False
    if cmd in ("/quit", "/exit", "/q"):
        console.print("[dim]再见。[/dim]")
        return True
    elif cmd == "/score":
        info = get_familiarity_info(OWNER_SESSION)
        console.print(
            Panel(
                f"好感度: [bold]{info['score']}[/bold] / 100\n"
                f"等级: [bold]{info['level']}[/bold]\n"
                f"上次更新: {info['updated_at'][:10]}",
                title="[debug] 好感度信息",
                style="yellow",
            )
        )
        return False
    elif cmd == "/history":
        messages = get_recent_messages(20, OWNER_SESSION)
        if not messages:
            console.print("[dim]还没有聊天记录。[/dim]")
        else:
            for m in messages:
                if m["role"] == "user":
                    console.print(f"[bold green]你:[/bold green] {m['content']}")
                else:
                    console.print(f"[bold cyan]仆仆:[/bold cyan] {m['content']}")
        return False
    elif cmd in ("/tasks", "/定时任务"):
        console.print(manage_scheduled_task(OWNER_SESSION, {"action": "list"}))
        return False
    elif command_name in ("/important", "/events", "/important_events", "/重要事件", "/记忆事件"):
        console.print(format_important_events_report(OWNER_SESSION, query=command_arg))
        return False
    elif cmd in ("/facts", "/fact", "/memory_facts", "/长期记忆", "/事实记忆"):
        console.print(format_facts_report(OWNER_SESSION))
        return False
    elif command_name in ("/tidy", "/cleanup", "/整理记忆", "/整理"):
        tidy_mode, tidy_usage = _parse_tidy_mode(command_arg)
        if tidy_usage:
            console.print(tidy_usage)
            return False
        status_text = "[cyan]仆仆在检查 memU 长期记忆...[/cyan]" if tidy_mode == "check" else "[cyan]仆仆在整理 memU 长期记忆...[/cyan]"
        with console.status(status_text):
            report = run_memu_maintenance(OWNER_SESSION, mode=tidy_mode)
        console.print(report)
        return False
    elif command_name in ("/proactive", "/主动", "/主动消息"):
        action = command_arg.strip().lower()
        if action in {"", "status", "状态", "狀態"}:
            console.print("主动消息：" + ("已开启" if is_proactive_enabled() else "已关闭"))
        elif action in {"on", "enable", "enabled", "open", "start", "1", "true", "yes", "开启", "打开", "开"}:
            set_proactive_enabled(True)
            console.print("主动消息已开启。QQ 后台循环会在连接后运行；CLI 里只保存开关。")
        elif action in {"off", "disable", "disabled", "close", "stop", "0", "false", "no", "关闭", "关"}:
            set_proactive_enabled(False)
            console.print("主动消息已关闭。")
        else:
            console.print("用法：/proactive [status|on|off]")
        return False
    elif command_name in ("/recall", "/memu_recall", "/召回"):
        query = command_arg.strip()
        if not query:
            console.print("用法：/recall 想测试召回的内容")
        else:
            console.print(format_memu_recall_report(query, OWNER_SESSION))
        return False
    elif command_name in ("/memu_rebuild", "/rebuild_memory", "/重建记忆"):
        with console.status("[cyan]正在重建 memU 记忆索引...[/cyan]"):
            console.print(rebuild_memu_session(OWNER_SESSION))
        return False
    elif cmd == "/reset":
        confirm = console.input("[bold red]确认重置仆仆？所有记忆、好感度、聊天记录都会清空 (y/N): [/bold red]").strip().lower()
        if confirm == "y":
            reset_session(OWNER_SESSION)
            clear_memu_session(OWNER_SESSION)
            console.print("[bold red]已重置。仆仆回到了最初的状态。[/bold red]")
        else:
            console.print("[dim]取消重置。[/dim]")
        return False

    return False


def main():
    _configure_cli_instance_interactively()
    setup_runtime_logging()
    init_db()
    preflight_model_providers()
    print_banner()

    def _cli_followup_sender(text: str):
        console.print()
        console.print("[bold cyan]仆仆 (追问):[/bold cyan] ", end="")
        console.print(Markdown(text))
        console.print()

    register_sender(OWNER_SESSION, _cli_followup_sender)

    threading.Thread(target=_cli_scheduler_loop, daemon=True).start()

    while True:
        try:
            user_input = console.input("[bold green]你: [/bold green]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]再见。[/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            should_exit = handle_command(user_input)
            if should_exit:
                break
            continue

        with console.status("[cyan]仆仆正在想...[/cyan]"):
            try:
                reply = chat(user_input, OWNER_SESSION, is_admin=True)
            except Exception as e:
                console.print(f"[red]出错了: {e}[/red]")
                continue

        console.print(f"[bold cyan]仆仆:[/bold cyan] ", end="")
        console.print(Markdown(reply))
        console.print()


if __name__ == "__main__":
    main()
