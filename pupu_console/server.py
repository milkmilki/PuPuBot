"""FastAPI Web console for multi-instance PuPu."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from ipaddress import ip_address
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pupu.arbiter_runtime import get_shared_arbiter_runtime
from pupu.app_config import apply_app_config_env, default_instance_settings, ensure_app_config_file, load_app_config
from pupu.hooks import HookEvent, register_hook
from pupu.shared_runtime import async_shutdown_shared_runtime

from . import instance_store, souls_store
from .paths import instances_dir
from .process_manager import DESKTOP_SESSION_ID, ProcessManager

pm = ProcessManager()
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_DESKTOP_HOOK_NAMES = (
    "instance.status",
    "desktop.message",
    "chat.started",
    "chat.reply_created",
    "chat.error",
    "memory.review_started",
    "memory.review_finished",
)
_desktop_event_queues: list[asyncio.Queue[dict[str, Any]]] = []
_desktop_event_loop: asyncio.AbstractEventLoop | None = None
_desktop_hook_unsubscribers: list[Any] = []
_API_SECRET_FIELDS = {
    "llm.anthropic.api_key",
    "llm.deepseek.api_key",
    "llm.xiaoshuoai.api_key",
    "semantic_index.embed_api_key",
    "mcp.servers.tavily.env.TAVILY_API_KEY",
}
_API_VALUE_FIELDS = {
    "llm.provider",
    "llm.chat_provider",
    "llm.judge_provider",
    "llm.maintenance_provider",
    "llm.proactive_provider",
    "llm.anthropic.base_url",
    "llm.deepseek.base_url",
    "llm.xiaoshuoai.base_url",
    "semantic_index.embed_base_url",
}
_API_SETTING_FIELDS = _API_SECRET_FIELDS | _API_VALUE_FIELDS
_shutdown_lock = threading.Lock()
_shutdown_scheduled = False


def _nested_get(data: dict[str, Any], dotted_key: str) -> Any:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _nested_set(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    cur = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = cur.get(part)
        if not isinstance(child, dict):
            child = {}
            cur[part] = child
        cur = child
    cur[parts[-1]] = value


def _mask_secret(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {"configured": False, "preview": ""}
    suffix = text[-4:] if len(text) >= 4 else text
    return {"configured": True, "preview": f"••••{suffix}"}


def _write_app_config(config_path: Path, cfg: dict[str, Any]) -> None:
    from pupu import app_config

    if app_config.yaml is None:
        raise RuntimeError("PyYAML is required to write pupu.yaml")
    config_path.write_text(
        app_config.yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _mark_console_shutdown_scheduled() -> bool:
    global _shutdown_scheduled
    with _shutdown_lock:
        if _shutdown_scheduled:
            return False
        _shutdown_scheduled = True
        return True


def _shutdown_console_process(delay_seconds: float = 0.35) -> None:
    time.sleep(delay_seconds)
    try:
        pm.stop_all()
    except Exception:
        pass
    os._exit(0)


def _netstat_listener_pids(port: int) -> list[int]:
    if os.name != "nt":
        return []
    output = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=0x08000000,
    )
    pids: set[int] = set()
    port_suffix = f":{int(port)}"
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if not parts[1].endswith(port_suffix):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        try:
            pids.add(int(parts[4]))
        except ValueError:
            continue
    return sorted(pids)


def _clear_external_port_listeners(port: int) -> list[int]:
    if not (1 <= int(port) <= 65535):
        return []
    current_pid = os.getpid()
    killed: list[int] = []
    for pid in _netstat_listener_pids(int(port)):
        if pid == current_pid:
            continue
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=0x08000000,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"failed to clear stale listener on port {port} pid={pid}: {detail}")
        killed.append(pid)
    if killed:
        time.sleep(0.25)
    return killed


def _desktop_event_from_hook(event: HookEvent) -> dict[str, Any]:
    return {
        "name": event.name,
        "created_at": event.created_at,
        "instance_id": event.instance_id,
        "payload": dict(event.payload),
    }


def _publish_desktop_event(event: HookEvent) -> None:
    loop = _desktop_event_loop
    if loop is None:
        return
    message = _desktop_event_from_hook(event)
    for queue in list(_desktop_event_queues):
        loop.call_soon_threadsafe(queue.put_nowait, message)


def _register_desktop_hooks(loop: asyncio.AbstractEventLoop) -> None:
    global _desktop_event_loop
    _desktop_event_loop = loop
    if _desktop_hook_unsubscribers:
        return
    for name in _DESKTOP_HOOK_NAMES:
        _desktop_hook_unsubscribers.append(register_hook(name, _publish_desktop_event))


def _unregister_desktop_hooks() -> None:
    global _desktop_event_loop
    while _desktop_hook_unsubscribers:
        unregister = _desktop_hook_unsubscribers.pop()
        try:
            unregister()
        except Exception:
            pass
    _desktop_event_loop = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    loop = asyncio.get_running_loop()
    pm.set_event_loop(loop)
    _register_desktop_hooks(loop)
    try:
        yield
    finally:
        _unregister_desktop_hooks()
        pm.stop_all()
        await async_shutdown_shared_runtime()
        pm.set_event_loop(None)


app = FastAPI(title="PuPu Console", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/api/arbiter")
def api_arbiter_get() -> dict[str, Any]:
    st = get_shared_arbiter_runtime().status()
    return {
        "running": True,
        "pid": st.get("pid"),
        "runtime": "embedded",
        "pending_groups": st.get("pending_groups", []),
        "audit_log": st.get("audit_log") or str(instances_dir() / "_shared" / "arbiter_audit.log"),
        "db_path": st.get("db_path") or str(instances_dir() / "_shared" / "arbiter.db"),
    }


@app.get("/")
def index_page() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _instance_summary(instance_id: str) -> dict[str, Any]:
    st = pm.status(instance_id)
    cfg, _ = instance_store.read_instance_files(instance_id)
    port = int(cfg.get("port", instance_store.read_port(instance_store.instance_dir(instance_id))))
    return {
        "id": instance_id,
        "display_name": cfg.get("display_name", instance_id),
        "port": port,
        "qq_mode": cfg.get("qq_mode", "napcat"),
        "running": st["running"],
        "pid": st.get("pid"),
        "runtime": st.get("runtime"),
    }


def _validate_napcat_self_id_binding(instance_id: str, cfg: dict[str, Any]) -> None:
    if str(cfg.get("qq_mode") or "").strip().lower() != "napcat":
        return
    bot_id = str(cfg.get("bot_id") or "").strip()
    if bot_id.isdigit():
        return

    napcat_count = 0
    for iid in instance_store.list_instance_ids():
        try:
            other_cfg, _ = instance_store.read_instance_files(iid)
        except Exception:
            continue
        if str(other_cfg.get("qq_mode") or "").strip().lower() == "napcat":
            napcat_count += 1
    if napcat_count <= 1:
        return

    display = str(cfg.get("display_name") or instance_id).strip()
    raise RuntimeError(
        f"检测到多个 NapCat 实例，启动 {display} 前必须在灵魂设置里填写数字 Bot QQ / self_id，"
        "用于防止 NapCat 连错端口时串号"
    )


@app.get("/api/instances")
def api_list_instances() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for iid in instance_store.list_instance_ids():
        try:
            out.append(_instance_summary(iid))
        except Exception:
            continue
    return out


@app.get("/api/desktop/status")
def api_desktop_status() -> dict[str, Any]:
    instances = api_list_instances()
    running = [item for item in instances if item.get("running")]
    selected = running[0] if running else (instances[0] if instances else None)
    return {
        "instances": instances,
        "selected_instance_id": selected.get("id") if selected else "",
        "running": bool(running),
        "session_id": DESKTOP_SESSION_ID,
    }


@app.post("/api/desktop/shutdown-console")
def api_shutdown_desktop_console(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="console shutdown is only allowed from localhost")
    scheduled = _mark_console_shutdown_scheduled()
    if scheduled:
        background_tasks.add_task(_shutdown_console_process)
    return {
        "ok": True,
        "scheduled": scheduled,
        "message": "PuPu Console is shutting down." if scheduled else "PuPu Console shutdown is already scheduled.",
    }


@app.get("/api/desktop/settings/api-keys")
def api_get_desktop_api_key_settings() -> dict[str, Any]:
    config_path, _ = ensure_app_config_file()
    cfg = load_app_config(config_path)
    values: dict[str, str] = {}
    secrets: dict[str, dict[str, Any]] = {}
    for field in sorted(_API_VALUE_FIELDS):
        raw = _nested_get(cfg, field)
        values[field] = "" if raw is None else str(raw)
    for field in sorted(_API_SECRET_FIELDS):
        secrets[field] = _mask_secret(_nested_get(cfg, field))
    return {
        "config_path": str(config_path),
        "providers": ["deepseek", "anthropic", "xiaoshuoai"],
        "values": values,
        "secrets": secrets,
    }


@app.put("/api/desktop/settings/api-keys")
def api_put_desktop_api_key_settings(body: dict[str, Any]) -> dict[str, Any]:
    raw_values = body.get("values")
    if not isinstance(raw_values, dict):
        raise HTTPException(status_code=400, detail="expected values object")
    unknown = sorted(str(key) for key in raw_values if str(key) not in _API_SETTING_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported setting: {unknown[0]}")

    config_path, _ = ensure_app_config_file()
    cfg = load_app_config(config_path)
    for key, raw in raw_values.items():
        field = str(key)
        value = str(raw or "").strip()
        if field in _API_SECRET_FIELDS and not value:
            continue
        _nested_set(cfg, field, value)

    try:
        _write_app_config(config_path, cfg)
        apply_app_config_env(override=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"save settings failed: {e}") from e
    return api_get_desktop_api_key_settings()


@app.post("/api/desktop/chat")
async def api_desktop_chat(body: dict[str, Any]) -> dict[str, Any]:
    instance_id = str(body.get("instance_id") or "").strip()
    text = str(body.get("text") or "").strip()
    if not instance_id:
        raise HTTPException(status_code=400, detail="missing instance_id")
    if not text:
        raise HTTPException(status_code=400, detail="missing text")
    try:
        instance_store.validate_instance_id(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not instance_store.instance_dir(instance_id).is_dir():
        raise HTTPException(status_code=404, detail="unknown instance")
    if not pm.status(instance_id)["running"]:
        raise HTTPException(status_code=409, detail="instance is not running")
    try:
        reply = await pm.desktop_chat(instance_id, text)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "instance_id": instance_id,
        "session_id": DESKTOP_SESSION_ID,
        "reply": reply,
    }


@app.post("/api/instances")
def api_create_instance(body: dict[str, Any]) -> dict[str, Any]:
    apply_app_config_env()
    defaults = default_instance_settings()
    display_name = str(body.get("display_name") or defaults["display_name"]).strip() or defaults["display_name"]
    port = body.get("port")
    qq_mode = str(body.get("qq_mode") or defaults["qq_mode"]).strip()
    soul_slug = body.get("soul_slug")
    if soul_slug is not None:
        soul_slug = str(soul_slug).strip() or None
    try:
        iid = instance_store.create_instance(
            display_name,
            port=int(port) if port is not None else None,
            qq_mode=qq_mode,
            soul_slug=soul_slug,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": iid, **_instance_summary(iid)}


@app.get("/api/instances/{instance_id}")
def api_get_instance(instance_id: str) -> dict[str, Any]:
    try:
        cfg, persona = instance_store.read_instance_files(instance_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    st = pm.status(instance_id)
    cfg = dict(cfg)
    cfg["persona"] = persona
    cfg["running"] = st["running"]
    cfg["pid"] = st.get("pid")
    cfg["runtime"] = st.get("runtime")
    mp = instance_store.memory_db_path(instance_id)
    cfg["memory_path"] = str(mp)
    cfg["memory_db_exists"] = mp.is_file()
    return cfg


@app.put("/api/instances/{instance_id}")
def api_put_instance(instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
    cfg_patch = body.get("instance") or body.get("config")
    persona_patch = body.get("persona")
    if cfg_patch is None and persona_patch is None:
        raise HTTPException(status_code=400, detail="expected instance or persona patch")
    try:
        cfg, persona = instance_store.merge_update(
            instance_id,
            dict(cfg_patch) if isinstance(cfg_patch, dict) else None,
            dict(persona_patch) if isinstance(persona_patch, dict) else None,
        )
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    out = dict(cfg)
    out["persona"] = persona
    return out


@app.delete("/api/instances/{instance_id}")
def api_delete_instance(instance_id: str) -> dict[str, str]:
    try:
        pm.stop(instance_id)
    except Exception:
        pass
    try:
        instance_store.delete_instance(instance_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": "true"}


@app.post("/api/instances/{instance_id}/start")
def api_start_instance(instance_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        instance_store.instance_dir(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not instance_store.instance_dir(instance_id).is_dir():
        raise HTTPException(status_code=404, detail="unknown instance")
    launch_mode = None
    if isinstance(body, dict) and body.get("qq_mode") is not None:
        launch_mode = str(body.get("qq_mode") or "").strip().lower()
        if launch_mode not in {"cli", "napcat", "siri"}:
            raise HTTPException(status_code=400, detail="qq_mode must be cli, napcat or siri")
        if pm.status(instance_id).get("running"):
            raise HTTPException(status_code=409, detail="stop instance before changing launch mode")
        try:
            instance_store.merge_update(instance_id, {"qq_mode": launch_mode}, None)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    cleared_port_pids: list[int] = []
    if not pm.status(instance_id).get("running"):
        try:
            cfg, _ = instance_store.read_instance_files(instance_id)
            _validate_napcat_self_id_binding(instance_id, cfg)
            port = int(cfg.get("port") or instance_store.read_port(instance_store.instance_dir(instance_id)))
            cleared_port_pids = _clear_external_port_listeners(port)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception:
            cleared_port_pids = []
    try:
        pid = pm.start(instance_id)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"pid": pid, "cleared_port_pids": cleared_port_pids, **_instance_summary(instance_id)}


@app.post("/api/instances/{instance_id}/stop")
def api_stop_instance(instance_id: str) -> dict[str, Any]:
    pm.stop(instance_id)
    return _instance_summary(instance_id)


@app.get("/api/instances/{instance_id}/logs")
def api_instance_logs(instance_id: str, tail: int = 200) -> dict[str, str]:
    try:
        instance_store.validate_instance_id(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    text = pm.tail_console_log(instance_id, n=max(1, min(tail, 5000)))
    return {"text": text}


@app.get("/api/instances/{instance_id}/memory_path")
def api_memory_path(instance_id: str) -> dict[str, Any]:
    try:
        p = instance_store.memory_db_path(instance_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    sp = str(p)
    return {
        "memory_path": sp,
        "exists": p.is_file(),
    }


@app.post("/api/instances/{instance_id}/import_memory")
async def api_import_memory(
    instance_id: str,
    file: UploadFile = File(..., description="SQLite memory file (.db)"),
) -> dict[str, Any]:
    try:
        instance_store.validate_instance_id(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not instance_store.instance_dir(instance_id).is_dir():
        raise HTTPException(status_code=404, detail="unknown instance")
    if pm.status(instance_id)["running"]:
        raise HTTPException(
            status_code=409,
            detail="实例运行中无法导入记忆，请先停止实例。",
        )

    name = (file.filename or "").lower()
    if name and not (name.endswith(".db") or name.endswith(".sqlite")):
        raise HTTPException(
            status_code=400,
            detail="请上传 .db 或 .sqlite 文件",
        )

    import tempfile

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".db",
            delete=False,
        ) as tmp:
            body = await file.read()
            tmp.write(body)
            tmp_path = Path(tmp.name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"upload failed: {e}") from e

    try:
        final = instance_store.replace_memory_db(instance_id, tmp_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "ok": True,
        "memory_path": str(final),
        "message": "记忆已覆盖导入（SQLite 替换完成）。",
    }


@app.post("/api/instances/{instance_id}/apply_soul")
def api_apply_soul(instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
    slug = str(body.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="missing slug")
    try:
        cfg, persona = souls_store.apply_to_instance(slug, instance_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    out = dict(cfg)
    out["persona"] = persona
    return out


@app.get("/api/souls")
def api_list_souls() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for slug in souls_store.list_soul_slugs():
        try:
            s = souls_store.load_soul(slug)
            out.append(
                {
                    "slug": slug,
                    "display_name": s.get("display_name", slug),
                }
            )
        except Exception:
            continue
    return out


@app.get("/api/souls/{slug}")
def api_get_soul(slug: str) -> dict[str, Any]:
    try:
        return souls_store.load_soul(slug)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/souls")
def api_post_soul(body: dict[str, Any]) -> dict[str, Any]:
    slug = str(body.get("slug") or "").strip()
    display_name = str(body.get("display_name") or slug).strip()
    from_instance = body.get("from_instance_id")
    if from_instance:
        from_instance = str(from_instance).strip()
        try:
            soul = souls_store.capture_from_instance(from_instance, slug, display_name)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return soul
    if not slug:
        raise HTTPException(status_code=400, detail="missing slug")
    try:
        souls_store.save_soul(slug, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return souls_store.load_soul(slug)


@app.put("/api/souls/{slug}")
def api_put_soul(slug: str, body: dict[str, Any]) -> dict[str, Any]:
    data = dict(body)
    data["slug"] = slug
    try:
        souls_store.save_soul(slug, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return souls_store.load_soul(slug)


@app.delete("/api/souls/{slug}")
def api_delete_soul(slug: str) -> dict[str, str]:
    try:
        souls_store.delete_soul(slug)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": "true"}


@app.websocket("/ws/instances/{instance_id}/console")
async def ws_console(instance_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    queue: asyncio.Queue[str] = asyncio.Queue()
    pm.register_queue(instance_id, queue)
    try:
        while True:
            line = await queue.get()
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        pm.unregister_queue(instance_id, queue)


@app.websocket("/ws/desktop/events")
async def ws_desktop_events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _desktop_event_queues.append(queue)
    try:
        await websocket.send_json(
            {
                "name": "desktop.connected",
                "created_at": "",
                "instance_id": "",
                "payload": {"session_id": DESKTOP_SESSION_ID},
            }
        )
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        if queue in _desktop_event_queues:
            _desktop_event_queues.remove(queue)
