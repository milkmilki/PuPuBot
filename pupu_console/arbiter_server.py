"""Standalone HTTP server for group arbitration (default 127.0.0.1:18079).

Run::

    python -m pupu_console.arbiter_server

Or::

    python run_arbitrator.py

Optional JSON config: ``instances/_shared/arbiter_server.json`` (see ``arbiter_server.json.example``).

Environment (override file): ``PUPU_ARBITER_HOST``, ``PUPU_ARBITER_PORT``, ``PUPU_JUDGE_PROVIDER``,
``PUPU_DEEPSEEK_MODEL``, ``PUPU_DEEPSEEK_API_KEY``, etc. Loads repo ``.env`` first.

Debounce (``PUPU_ARBITER_DEBOUNCE_IDLE_SEC`` / ``PUPU_ARBITER_DEBOUNCE_MAX_SEC`` or JSON keys
``debounce_idle_seconds`` / ``debounce_max_seconds``):

- **Idle**: seconds of quiet after the last ``/api/observe`` before ``run_judge`` (default 30, clamped
  to 0.1–3600). JSON ``arbiter_server.json`` overrides via env at startup.
- **Hard cap**: max seconds since the first observe in a burst before forcing ``run_judge`` even if
  chat never goes quiet (default 60, clamped to 0.5–86400 when enabled). Set to **unlimited** with
  ``none`` / ``off`` / ``unlimited`` / ``inf`` (case-insensitive), JSON ``null`` on
  ``debounce_max_seconds``, or numeric ``-1`` — then only the idle window applies.

Routes:
    POST /api/observe          — push one observed group message; idempotent
    GET  /api/await_decision   — long-poll for the next decision
    GET  /api/group_silence    — query per-group forced ``speaker=none`` (``/silence``) flag
    POST /api/group_silence   — set that flag (body: ``group_id``, ``enabled`` bool)
    POST /api/group_arbitrate  — legacy single-call arbitration (kept for the 30-day deprecation window)
    GET  /health
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .paths import get_repo_root, instances_dir


def _config_path() -> Path:
    return instances_dir() / "_shared" / "arbiter_server.json"


def load_arbiter_server_config() -> dict:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[arbiter_server] failed to read {path}: {exc}", file=sys.stderr)
        return {}


def apply_arbiter_settings(cfg: dict) -> None:
    """Map JSON keys to PuPu LLM environment variables (only non-empty values)."""
    if not cfg:
        return
    mapping = {
        "judge_provider": "PUPU_JUDGE_PROVIDER",
        "judge_model": "PUPU_JUDGE_MODEL",
        "deepseek_model": "PUPU_DEEPSEEK_MODEL",
        "arbiter_judge_model": "PUPU_ARBITER_JUDGE_MODEL",
        "deepseek_base_url": "PUPU_DEEPSEEK_BASE_URL",
        "deepseek_api_key": "PUPU_DEEPSEEK_API_KEY",
        "debounce_idle_seconds": "PUPU_ARBITER_DEBOUNCE_IDLE_SEC",
        "debounce_max_seconds": "PUPU_ARBITER_DEBOUNCE_MAX_SEC",
    }
    for key, env_name in mapping.items():
        if key not in cfg:
            continue
        val = cfg[key]
        if val is None:
            # JSON null on debounce_max_seconds disables the hard cap (idle-only debounce).
            if key == "debounce_max_seconds":
                os.environ["PUPU_ARBITER_DEBOUNCE_MAX_SEC"] = "none"
            continue
        text = str(val).strip()
        if text:
            os.environ[env_name] = text


def resolve_bind(cfg: dict) -> tuple[str, int]:
    host = os.environ.get("PUPU_ARBITER_HOST", "").strip()
    if not host:
        host = str(cfg.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("PUPU_ARBITER_PORT", "").strip()
    if port_raw:
        port = int(port_raw)
    else:
        port = int(cfg.get("port") or 18079)
    return host, port


_DEBOUNCE_IDLE_MAX = 3600.0
_DEBOUNCE_CAP_MAX = 86400.0
_DEBOUNCE_MAX_DISABLED_TOKENS = frozenset(
    {"none", "off", "no", "false", "unlimited", "inf", "infinity"}
)


def _debounce_idle_seconds() -> float:
    raw = os.environ.get("PUPU_ARBITER_DEBOUNCE_IDLE_SEC", "").strip()
    try:
        return max(0.1, min(_DEBOUNCE_IDLE_MAX, float(raw))) if raw else 30.0
    except ValueError:
        return 30.0


def _debounce_max_seconds() -> float | None:
    """Hard cap from first observe in a burst; ``None`` = no cap (idle-only debounce)."""
    raw = os.environ.get("PUPU_ARBITER_DEBOUNCE_MAX_SEC", "").strip()
    if not raw:
        return 60.0
    lower = raw.lower()
    if lower in _DEBOUNCE_MAX_DISABLED_TOKENS:
        return None
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    if value < 0:
        return None
    return max(0.5, min(_DEBOUNCE_CAP_MAX, value))


def _await_decision_max_timeout() -> float:
    raw = os.environ.get("PUPU_ARBITER_AWAIT_MAX_TIMEOUT_SEC", "").strip()
    try:
        return max(1.0, min(120.0, float(raw))) if raw else 60.0
    except ValueError:
        return 60.0


# ---------------------------------------------------------------------------
# Per-group debounce watchdog
# ---------------------------------------------------------------------------


class _DebounceWatchdog:
    """One asyncio task per group; resettable idle timer with an optional hard cap.

    Lifecycle:
      - First observe arrives -> ``schedule(group_id)`` creates the task and
        records ``opened_at = now``.
      - Subsequent observes -> ``schedule(group_id)`` cancels the existing
        idle wait and starts a new one, but does NOT reset ``opened_at`` so
        the hard cap can still fire (when enabled).
      - When either the idle window or the hard cap elapses, the task fires
        ``run_judge(group_id)`` in a worker thread, then drops state.
      - If the hard cap is disabled (``debounce_max`` unlimited), only the idle
        window applies after the last observe in a burst.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def schedule(self, group_id: str) -> None:
        async with self._lock:
            existing = self._tasks.get(group_id)
            if existing and not existing.done():
                existing.cancel()
            self._opened_at.setdefault(group_id, asyncio.get_running_loop().time())
            self._tasks[group_id] = asyncio.create_task(self._run(group_id))

    async def _run(self, group_id: str) -> None:
        idle = _debounce_idle_seconds()
        cap = _debounce_max_seconds()
        loop = asyncio.get_running_loop()
        opened_at = self._opened_at.get(group_id, loop.time())
        if cap is None:
            sleep_for = idle
        else:
            # Sleep at most until the hard cap, but wake up after ``idle``.
            max_remaining = max(0.0, opened_at + cap - loop.time())
            sleep_for = min(idle, max_remaining) if max_remaining > 0 else idle
        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            return
        await self._fire(group_id)

    async def _fire(self, group_id: str) -> None:
        async with self._lock:
            self._tasks.pop(group_id, None)
            self._opened_at.pop(group_id, None)
        try:
            from . import arbitrator

            decision = await asyncio.to_thread(arbitrator.run_judge, group_id, source="debounce")
            if decision is None:
                # Nothing to judge yet: silently drop. New observes will start a new window.
                return
        except Exception as exc:
            print(
                f"[arbiter_server] debounce judge failed group={group_id} err={type(exc).__name__}: {exc}",
                file=sys.stderr,
            )


_watchdog: _DebounceWatchdog | None = None


def _get_watchdog() -> _DebounceWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = _DebounceWatchdog()
    return _watchdog


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def build_app(bind_host: str, bind_port: int):
    from fastapi import FastAPI, Query

    from . import arbitrator

    app = FastAPI(title="PuPu Group Arbiter", version="2.0.0")

    @app.get("/health")
    def health() -> dict:
        jp = os.environ.get("PUPU_JUDGE_PROVIDER", "").strip() or "anthropic"
        debounce_max = _debounce_max_seconds()
        return {
            "ok": True,
            "judge_provider": jp,
            "judge_model": (os.environ.get("PUPU_JUDGE_MODEL") or "").strip() or None,
            "arbiter_judge_model": (os.environ.get("PUPU_ARBITER_JUDGE_MODEL") or "").strip() or None,
            "deepseek_model": (os.environ.get("PUPU_DEEPSEEK_MODEL") or "").strip() or None,
            "bind": f"{bind_host}:{bind_port}",
            "debounce_idle_sec": _debounce_idle_seconds(),
            "debounce_max_sec": debounce_max,
            "debounce_max_unlimited": debounce_max is None,
        }

    @app.post("/api/observe")
    async def api_observe(body: dict) -> dict:
        result = await asyncio.to_thread(arbitrator.observe, body)
        if result.get("ok"):
            group_id = str(body.get("group_id") or "").strip()
            if group_id:
                # Pre-create the asyncio.Event in the server loop so any
                # concurrent await_decision calls see it. ``observe`` itself
                # runs in a worker thread and can't touch the loop.
                arbitrator._ensure_group_event(group_id)
                await _get_watchdog().schedule(group_id)
        return result

    @app.get("/api/await_decision")
    async def api_await_decision(
        group_id: str = Query(...),
        since: int = Query(0, ge=0),
        timeout: float = Query(30.0, ge=0.0),
    ) -> dict:
        timeout = min(timeout, _await_decision_max_timeout())
        # Make sure the Event exists in this loop before we wait.
        arbitrator._ensure_group_event(group_id)
        decision = await arbitrator.await_decision_async(group_id, int(since), float(timeout))
        if decision is None:
            return {"ok": True, "decision": None, "since": int(since)}
        return {"ok": True, "decision": decision}

    @app.get("/api/group_silence")
    def api_group_silence_get(group_id: str = Query(...)) -> dict:
        gid = str(group_id or "").strip()
        if not gid:
            return {"ok": False, "error": "missing_group_id"}
        return {
            "ok": True,
            "group_id": gid,
            "enabled": arbitrator.is_group_arbitration_silenced(gid),
        }

    @app.post("/api/group_silence")
    async def api_group_silence_post(body: dict) -> dict:
        gid = str(body.get("group_id") or "").strip()
        if not gid:
            return {"ok": False, "error": "missing_group_id"}
        raw_en = body.get("enabled")
        if isinstance(raw_en, str):
            lo = raw_en.strip().lower()
            if lo in {"1", "true", "yes", "y", "on", "开", "开启"}:
                enabled = True
            elif lo in {"0", "false", "no", "n", "off", "关", "关闭"}:
                enabled = False
            else:
                return {"ok": False, "error": "invalid_enabled"}
        else:
            enabled = bool(raw_en)
        return await asyncio.to_thread(arbitrator.set_group_arbitration_silence, gid, enabled)

    @app.post("/api/group_arbitrate")
    async def api_group_arbitrate(body: dict) -> dict:
        # Legacy compatibility entry; kept during the deprecation window.
        return await asyncio.to_thread(arbitrator.arbitrate, body)

    return app


def main() -> None:
    repo = get_repo_root()
    load_dotenv(repo / ".env")
    cfg = load_arbiter_server_config()
    apply_arbiter_settings(cfg)
    host, port = resolve_bind(cfg)

    jp = os.environ.get("PUPU_JUDGE_PROVIDER", "").strip() or "(default anthropic)"
    print(f"[arbiter_server] repo={repo}")
    print(f"[arbiter_server] bind http://{host}:{port}")
    print(
        f"[arbiter_server] audit_log={instances_dir() / '_shared' / 'arbiter_audit.log'} "
        f"(disable: PUPU_ARBITER_AUDIT=0); "
        f"judge_errors={instances_dir() / '_shared' / '错误.log'}"
    )
    print(f"[arbiter_server] config_file={_config_path()} exists={_config_path().is_file()}")
    print(f"[arbiter_server] PUPU_JUDGE_PROVIDER={jp}")
    cap = _debounce_max_seconds()
    cap_label = "unlimited" if cap is None else f"{cap}s"
    print(
        f"[arbiter_server] debounce idle={_debounce_idle_seconds()}s "
        f"max={cap_label}"
    )
    if os.environ.get("PUPU_JUDGE_PROVIDER", "").strip() == "deepseek":
        dm = os.environ.get("PUPU_DEEPSEEK_MODEL", "").strip() or "deepseek-v4-pro (default)"
        print(f"[arbiter_server] PUPU_DEEPSEEK_MODEL={dm}")
    has_key = bool(
        (os.environ.get("PUPU_DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    )
    if os.environ.get("PUPU_JUDGE_PROVIDER", "").strip() == "deepseek" and not has_key:
        print(
            "[arbiter_server] warning: set PUPU_DEEPSEEK_API_KEY (or ANTHROPIC_API_KEY) in .env for DeepSeek",
            file=sys.stderr,
        )

    import uvicorn

    app = build_app(host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
