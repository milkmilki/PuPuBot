"""Start and stop PuPu instance actors for the web console."""

from __future__ import annotations

import asyncio
import os
import threading
from collections import defaultdict

from pupu.actor import InstanceActor
from pupu.app_config import apply_app_config_env, ensure_app_config_file

from . import instance_store

DESKTOP_SESSION_ID = "desktop_owner"


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._actors: dict[str, InstanceActor] = {}
        self._actor_tasks: dict[str, asyncio.Future] = {}
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

    def get_actor(self, instance_id: str) -> InstanceActor | None:
        with self._lock:
            actor = self._actors.get(instance_id)
        if actor is not None and actor.running:
            return actor
        return None

    async def desktop_chat(self, instance_id: str, text: str) -> str:
        actor = self.get_actor(instance_id)
        if actor is None:
            raise RuntimeError("instance is not running")

        from pupu.agent import chat
        from pupu.instance_context import activate_instance_context

        cleaned = str(text or "").strip()
        with activate_instance_context(actor.context):
            reply = await asyncio.to_thread(
                chat,
                cleaned,
                DESKTOP_SESSION_ID,
                True,
                context_session=DESKTOP_SESSION_ID,
                identity_session=DESKTOP_SESSION_ID,
                speaker_key="owner",
                speaker_name="Desktop User",
            )
        return str(reply or "")

    def tail_console_log(self, instance_id: str, n: int = 200) -> str:
        path = instance_store.console_log_path(instance_id)
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
