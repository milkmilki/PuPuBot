"""Embedded group-chat arbitration runtime.

The arbitration data model and judge live in :mod:`pupu_console.arbitrator`.
This module owns process-local observe/debounce/decision tasks for open groups.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any


class EmbeddedArbiterRuntime:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def observe(self, payload: dict[str, Any], *, debounce_seconds: float) -> dict[str, Any] | None:
        from pupu_console import arbitrator

        result = await asyncio.to_thread(arbitrator.observe, payload)
        if result.get("ok"):
            group_id = str(payload.get("group_id") or "").strip()
            if group_id:
                arbitrator._ensure_group_event(group_id)
                await self.schedule(group_id, debounce_seconds=debounce_seconds)
        return result if isinstance(result, dict) else None

    async def schedule(self, group_id: str, *, debounce_seconds: float) -> None:
        group_id = str(group_id or "").strip()
        if not group_id:
            return
        delay = max(0.1, min(600.0, float(debounce_seconds)))
        async with self._lock:
            existing = self._tasks.get(group_id)
            if existing and not existing.done():
                existing.cancel()
            self._tasks[group_id] = asyncio.create_task(self._run_after_idle(group_id, delay))

    async def _run_after_idle(self, group_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self.run_judge(group_id)

    async def run_judge(self, group_id: str) -> dict[str, Any] | None:
        from pupu_console import arbitrator

        async with self._lock:
            self._tasks.pop(group_id, None)
        try:
            return await asyncio.to_thread(arbitrator.run_judge, group_id, source="embedded")
        except Exception as exc:
            print(f"[pupu][arbiter] embedded judge failed group={group_id} err={type(exc).__name__}: {exc}")
            return None

    async def await_decision(
        self,
        group_id: str,
        since: int,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        from pupu_console import arbitrator

        group_id = str(group_id or "").strip()
        if not group_id:
            return None
        arbitrator._ensure_group_event(group_id)
        return await arbitrator.await_decision_async(group_id, int(since), float(timeout))

    def is_silenced(self, group_id: str) -> bool:
        from pupu_console import arbitrator

        return arbitrator.is_group_arbitration_silenced(group_id)

    def set_silence(self, group_id: str, enabled: bool) -> dict[str, Any]:
        from pupu_console import arbitrator

        group_id = str(group_id or "").strip()
        if enabled and group_id:
            self.cancel_group(group_id)
        return arbitrator.set_group_arbitration_silence(group_id, enabled)

    def cancel_group(self, group_id: str) -> None:
        group_id = str(group_id or "").strip()
        if not group_id:
            return
        task = self._tasks.pop(group_id, None)
        if task and not task.done():
            task.cancel()

    def status(self) -> dict[str, Any]:
        from pupu_console.paths import instances_dir

        return {
            "running": True,
            "pid": os.getpid(),
            "runtime": "embedded",
            "pending_groups": sorted(self._tasks.keys()),
            "audit_log": str(instances_dir() / "_shared" / "arbiter_audit.log"),
            "db_path": str(instances_dir() / "_shared" / "arbiter.db"),
        }

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_ARBITER_RUNTIME = EmbeddedArbiterRuntime()


def get_shared_arbiter_runtime() -> EmbeddedArbiterRuntime:
    return _ARBITER_RUNTIME


async def close_shared_arbiter_runtime() -> None:
    await _ARBITER_RUNTIME.close()


__all__ = [
    "EmbeddedArbiterRuntime",
    "close_shared_arbiter_runtime",
    "get_shared_arbiter_runtime",
]
