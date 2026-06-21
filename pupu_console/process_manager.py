"""Start and stop PuPu instance actors for the web console."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path

from pupu.actor import InstanceActor
from pupu.app_config import apply_app_config_env, ensure_app_config_file

from . import instance_store
from .paths import get_repo_root, instances_dir


def _win_no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._actors: dict[str, InstanceActor] = {}
        self._actor_tasks: dict[str, asyncio.Future] = {}
        self._arbiter_proc: subprocess.Popen[str] | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._main_loop = loop

    def register_queue(self, instance_id: str, queue: asyncio.Queue[str]) -> None:
        self._queues[instance_id].append(queue)

    def unregister_queue(self, instance_id: str, queue: asyncio.Queue[str]) -> None:
        if instance_id in self._queues and queue in self._queues[instance_id]:
            self._queues[instance_id].remove(queue)

    def _emit_line(self, instance_id: str, text: str) -> None:
        loop = self._main_loop
        if loop is None:
            return
        for q in list(self._queues.get(instance_id, [])):
            loop.call_soon_threadsafe(q.put_nowait, text)

    def _append_console_log(self, instance_id: str, text: str) -> None:
        log_path = instance_store.console_log_path(instance_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log_f:
            log_f.write(text)
            log_f.flush()

    def _emit_actor_line(self, instance_id: str, text: str) -> None:
        try:
            self._append_console_log(instance_id, text)
        except Exception:
            pass
        self._emit_line(instance_id, text)

    def _arbiter_stdout_reader(self, proc: subprocess.Popen[str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if proc.stdout is None:
            return
        with log_path.open("a", encoding="utf-8", buffering=1) as log_f:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                log_f.write(line)
                log_f.flush()
        try:
            proc.stdout.close()
        except Exception:
            pass

    def start_arbiter(self) -> int:
        """Run ``python -m pupu_console.arbiter_server`` (repo root cwd)."""
        with self._lock:
            if self._arbiter_proc is not None and self._arbiter_proc.poll() is None:
                raise RuntimeError("arbiter service is already running")
            ensure_app_config_file()
            apply_app_config_env()
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            if sys.platform == "win32":
                env.setdefault("PYTHONUTF8", "1")
            proc = subprocess.Popen(
                [sys.executable, "-m", "pupu_console.arbiter_server"],
                cwd=str(get_repo_root()),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_win_no_window_flags(),
            )
            self._arbiter_proc = proc
            log_path = instances_dir() / "_shared" / "arbiter_console.log"
            threading.Thread(
                target=self._arbiter_stdout_reader,
                args=(proc, log_path),
                daemon=True,
            ).start()
            return proc.pid or 0

    def stop_arbiter(self, *, wait_s: float = 12.0) -> None:
        with self._lock:
            proc = self._arbiter_proc
            self._arbiter_proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32" and proc.pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=_win_no_window_flags(),
                        timeout=30,
                    )

    def arbiter_status(self) -> dict[str, object]:
        with self._lock:
            proc = self._arbiter_proc
        if proc is None:
            return {"running": False, "pid": None}
        code = proc.poll()
        if code is None:
            return {"running": True, "pid": proc.pid}
        self._arbiter_proc = None
        return {"running": False, "pid": None, "exit_code": code}

    def tail_arbiter_log(self, n: int = 200) -> str:
        path = instances_dir() / "_shared" / "arbiter_console.log"
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])

    def start(self, instance_id: str) -> int:
        loop = self._main_loop
        if loop is None:
            raise RuntimeError("actor runtime requires console event loop")
        with self._lock:
            if instance_id in self._actors:
                raise RuntimeError("already running")
            inst_dir = instance_store.instance_dir(instance_id)
            my_port = instance_store.read_port(inst_dir)
            for other_id in list(self._actors):
                if other_id == instance_id:
                    continue
                try:
                    op = instance_store.read_port(instance_store.instance_dir(other_id))
                except Exception:
                    continue
                if op == my_port:
                    raise RuntimeError(f"port {my_port} already used by running instance {other_id}")

            ensure_app_config_file()
            apply_app_config_env()
            actor = InstanceActor.from_instance_dir(
                inst_dir,
                emit_log=lambda text, iid=instance_id: self._emit_actor_line(iid, text),
            )
            future = asyncio.run_coroutine_threadsafe(actor.start(), loop)
            self._actors[instance_id] = actor
            self._actor_tasks[instance_id] = future
        try:
            future.result(timeout=30)
        except Exception:
            with self._lock:
                self._actors.pop(instance_id, None)
                self._actor_tasks.pop(instance_id, None)
            raise
        return os.getpid()

    def stop(self, instance_id: str, *, wait_s: float = 12.0) -> None:
        with self._lock:
            actor = self._actors.pop(instance_id, None)
            self._actor_tasks.pop(instance_id, None)
        if actor is None:
            return
        loop = self._main_loop
        if loop is not None:
            future = asyncio.run_coroutine_threadsafe(actor.stop(), loop)
            try:
                future.result(timeout=wait_s)
            except Exception:
                pass

    def stop_all(self) -> None:
        self.stop_arbiter()
        with self._lock:
            actor_ids = list(self._actors.keys())
        for iid in actor_ids:
            self.stop(iid)

    def status(self, instance_id: str) -> dict[str, object]:
        with self._lock:
            actor = self._actors.get(instance_id)
        if actor is not None:
            if actor.running:
                return {"running": True, "pid": os.getpid(), "runtime": "actor"}
            with self._lock:
                self._actors.pop(instance_id, None)
                self._actor_tasks.pop(instance_id, None)
        return {"running": False, "pid": None, "runtime": "actor"}

    def tail_console_log(self, instance_id: str, n: int = 200) -> str:
        path = instance_store.console_log_path(instance_id)
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
