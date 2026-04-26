"""Background runtime loops for backup and maintenance."""

from __future__ import annotations

import asyncio

from nonebot import get_driver

from pupu.backup import maybe_run_daily_backup
from pupu.maintenance import maybe_run_daily_maintenance

from . import state


async def maintenance_loop():
    while True:
        try:
            backup_report = await asyncio.to_thread(maybe_run_daily_backup)
            if backup_report:
                print(f"[pupu] auto backup\n{backup_report}")
            maintenance_report = await asyncio.to_thread(maybe_run_daily_maintenance)
            if maintenance_report:
                print(f"[pupu] auto maintenance\n{maintenance_report}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[pupu] maintenance loop failed: {exc}")
        await asyncio.sleep(45)


driver = get_driver()


@driver.on_startup
async def start_maintenance_loop():
    if state.maintenance_task is None or state.maintenance_task.done():
        state.maintenance_task = asyncio.create_task(maintenance_loop())
        print("[pupu] maintenance loop started")


@driver.on_shutdown
async def stop_maintenance_loop():
    if state.maintenance_task is not None:
        state.maintenance_task.cancel()
        state.maintenance_task = None
