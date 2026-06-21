"""Spawn and stop PuPu instance subprocesses."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path

from pupu.app_config import apply_app_config_env, ensure_app_config_file

from . import instance_store
from .paths import get_repo_root, instances_dir


def _win_no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen[str]] = {}
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

    def _stdout_reader(self, instance_id: str, proc: subprocess.Popen[str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if proc.stdout is None:
            return
        with log_path.open("a", encoding="utf-8", buffering=1) as log_f:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                log_f.write(line)
                log_f.flush()
                self._emit_line(instance_id, line)
        try:
            proc.stdout.close()
        except Exception:
            pass

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
                raise RuntimeError("仲裁服务已在运行")
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
        if proc is None:
            return
        if proc.poll() is not None:
            return
        pid = proc.pid
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
                if pid:
                    self._kill_process_tree_windows(pid)

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
        with self._lock:
            if instance_id in self._procs and self._procs[instance_id].poll() is None:
                raise RuntimeError("already running")

            inst_dir = instance_store.instance_dir(instance_id)
            my_port = instance_store.read_port(inst_dir)
            for other_id, proc in list(self._procs.items()):
                if other_id == instance_id or proc.poll() is not None:
                    continue
                try:
                    op = instance_store.read_port(instance_store.instance_dir(other_id))
                except Exception:
                    continue
                if op == my_port:
                    raise RuntimeError(f"port {my_port} already used by running instance {other_id}")

            ensure_app_config_file()
            apply_app_config_env()
            env = os.environ.copy()
            # Child stdout is decoded as UTF-8 below; on Chinese Windows the default
            # console encoding is often GBK, which would mojibake Chinese in console.log.
            env.setdefault("PYTHONIOENCODING", "utf-8")
            if sys.platform == "win32":
                env.setdefault("PYTHONUTF8", "1")

            proc = subprocess.Popen(
                [sys.executable, "-m", "pupu.instance_main", "--dir", str(inst_dir)],
                cwd=str(get_repo_root()),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_win_no_window_flags(),
            )
            self._procs[instance_id] = proc
            log_path = instance_store.console_log_path(instance_id)
            threading.Thread(
                target=self._stdout_reader,
                args=(instance_id, proc, log_path),
                daemon=True,
            ).start()
            return proc.pid or 0

    def _kill_process_tree_windows(self, pid: int) -> None:
        """Last resort: end the whole job object / child tree (NoneBot + uvicorn)."""
        if sys.platform != "win32" or not pid:
            return
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=_win_no_window_flags(),
            timeout=30,
        )

    def stop(self, instance_id: str, *, wait_s: float = 12.0) -> None:
        with self._lock:
            proc = self._procs.pop(instance_id, None)
        if proc is None:
            return
        if proc.poll() is not None:
            return
        pid = proc.pid
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
                if pid:
                    self._kill_process_tree_windows(pid)

    def stop_all(self) -> None:
        self.stop_arbiter()
        with self._lock:
            ids = list(self._procs.keys())
        for iid in ids:
            self.stop(iid)

    def status(self, instance_id: str) -> dict[str, object]:
        with self._lock:
            proc = self._procs.get(instance_id)
        if proc is None:
            return {"running": False, "pid": None}
        code = proc.poll()
        if code is None:
            return {"running": True, "pid": proc.pid}
        self._procs.pop(instance_id, None)
        return {"running": False, "pid": None, "exit_code": code}

    def tail_console_log(self, instance_id: str, n: int = 200) -> str:
        path = instance_store.console_log_path(instance_id)
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
