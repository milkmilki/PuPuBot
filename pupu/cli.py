"""Terminal chat interface with command handling and periodic scheduler tick."""

import threading
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent import chat
from .memory import get_event_log, get_familiarity_info, get_recent_messages, init_db, reset_session
from .tools import manage_scheduled_task

console = Console()

OWNER_SESSION = "owner"


def _cli_scheduler_loop():
    from pupu.scheduler import cli_scheduled_tasks_tick

    while True:
        time.sleep(45)
        try:
            cli_scheduled_tasks_tick()
        except Exception as e:
            print(f"[pupu] cli scheduler: {e}")


def print_banner():
    score_info = get_familiarity_info(OWNER_SESSION)
    console.print(
        Panel(
            f"[bold]仆仆[/bold] — 好感度: Lv.{score_info['level']}\n"
            f"输入消息开始聊天 | /quit 退出 | /score 好感度 | /history 最近聊天 | /tasks 定时任务",
            style="cyan",
        )
    )


def handle_command(cmd: str) -> bool:
    """Handle slash commands. Returns True if handled."""
    if cmd in ("/quit", "/exit", "/q"):
        console.print("[dim]再见。[/dim]")
        return True
    elif cmd == "/score":
        info = get_familiarity_info(OWNER_SESSION)
        events = get_event_log(10, OWNER_SESSION)
        console.print(
            Panel(
                f"好感度: [bold]{info['score']}[/bold] / 100\n"
                f"等级: [bold]{info['level']}[/bold]\n"
                f"上次更新: {info['updated_at'][:10]}",
                title="[debug] 好感度信息",
                style="yellow",
            )
        )
        if events:
            console.print("[yellow]最近事件:[/yellow]")
            for e in events:
                sign = "+" if e["delta"] > 0 else ""
                console.print(f"  {e['date'][:10]} [{sign}{e['delta']}] {e['description']}")
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
    elif cmd == "/reset":
        confirm = console.input("[bold red]确认重置仆仆？所有记忆、好感度、聊天记录都会清空 (y/N): [/bold red]").strip().lower()
        if confirm == "y":
            reset_session(OWNER_SESSION)
            console.print("[bold red]已重置。仆仆回到了最初的状态。[/bold red]")
        else:
            console.print("[dim]取消重置。[/dim]")
        return False

    return False


def main():
    init_db()
    print_banner()
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
