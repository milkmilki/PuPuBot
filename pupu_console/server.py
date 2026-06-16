"""FastAPI Web console for multi-instance PuPu."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pupu.app_config import apply_app_config_env, default_instance_settings

from . import arbitrator, instance_store, souls_store
from .paths import instances_dir
from .process_manager import ProcessManager

pm = ProcessManager()
_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(_: FastAPI):
    pm.set_event_loop(asyncio.get_running_loop())
    yield
    pm.stop_all()
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


def _arbiter_http_base() -> str:
    try:
        p = instances_dir() / "_shared" / "arbiter_server.json"
        if p.is_file():
            j = json.loads(p.read_text(encoding="utf-8"))
            h = str(j.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            port = int(j.get("port") or 18079)
            return f"http://{h}:{port}"
    except Exception:
        pass
    return "http://127.0.0.1:18079"


@app.get("/api/arbiter")
def api_arbiter_get() -> dict[str, Any]:
    st = pm.arbiter_status()
    base = _arbiter_http_base()
    return {
        "running": st.get("running", False),
        "pid": st.get("pid"),
        "exit_code": st.get("exit_code"),
        "bind": base,
        "health_url": f"{base}/health",
        "arbitrate_url": f"{base}/api/group_arbitrate",
        "audit_log": str(instances_dir() / "_shared" / "arbiter_audit.log"),
        "console_log": str(instances_dir() / "_shared" / "arbiter_console.log"),
    }


@app.post("/api/arbiter/start")
def api_arbiter_start() -> dict[str, Any]:
    try:
        pid = pm.start_arbiter()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    st = pm.arbiter_status()
    base = _arbiter_http_base()
    return {"pid": pid, "bind": base, "health_url": f"{base}/health", **st}


@app.post("/api/arbiter/stop")
def api_arbiter_stop() -> dict[str, Any]:
    pm.stop_arbiter()
    st = pm.arbiter_status()
    base = _arbiter_http_base()
    return {"bind": base, **st}


@app.get("/api/arbiter/logs")
def api_arbiter_logs(tail: int = 200) -> dict[str, str]:
    text = pm.tail_arbiter_log(n=max(1, min(tail, 5000)))
    return {"text": text}


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
    }


@app.get("/api/instances")
def api_list_instances() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for iid in instance_store.list_instance_ids():
        try:
            out.append(_instance_summary(iid))
        except Exception:
            continue
    return out


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
    mp = instance_store.memory_db_path(instance_id)
    memu_path = instance_store.memu_db_path(instance_id)
    cfg["memory_path"] = str(mp)
    cfg["memory_db_exists"] = mp.is_file()
    cfg["memu_path"] = str(memu_path)
    cfg["memu_db_exists"] = memu_path.is_file()
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
def api_start_instance(instance_id: str) -> dict[str, Any]:
    try:
        instance_store.instance_dir(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not instance_store.instance_dir(instance_id).is_dir():
        raise HTTPException(status_code=404, detail="unknown instance")
    try:
        pid = pm.start(instance_id)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"pid": pid, **_instance_summary(instance_id)}


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
        memu_path = instance_store.memu_db_path(instance_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    sp = str(p)
    return {
        "memory_path": sp,
        "exists": p.is_file(),
        "memu_path": str(memu_path),
        "memu_exists": memu_path.is_file(),
    }


def _empty_event_graph(path: Path, *, exists: bool) -> dict[str, Any]:
    return {
        "memory_path": str(path),
        "exists": exists,
        "threads": [],
        "steps": [],
        "nodes": [],
        "edges": [],
    }


def _event_graph_from_path(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_event_graph(path, exists=False)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        table_names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('event_threads', 'event_steps')"
            ).fetchall()
        }
        if "event_threads" not in table_names:
            return _empty_event_graph(path, exists=True)

        threads = [
            dict(row)
            for row in conn.execute(
                """
                SELECT t.id, t.session_id, t.key AS source_event_key, t.title, t.kind,
                       t.status, t.current_step_id, t.event_time, t.time_text,
                       t.followup_hint, t.confidence, t.linked_task_id,
                       t.search_text, t.merge_hint, t.created_at,
                       t.updated_at AS last_seen_at,
                       s.summary AS current_summary,
                       s.cause AS current_cause,
                       s.reflection AS current_reflection
                FROM event_threads t
                LEFT JOIN event_steps s ON s.id = t.current_step_id
                ORDER BY t.updated_at DESC, t.id DESC
                """
            ).fetchall()
        ]
        thread_ids = [int(row["id"]) for row in threads]
        steps: list[dict[str, Any]] = []
        if "event_steps" in table_names and thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            steps = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT id, thread_id, step_type, summary, cause, reflection,
                           occurred_at, source_msg_start_id, source_msg_end_id, created_at
                    FROM event_steps
                    WHERE thread_id IN ({placeholders})
                    ORDER BY thread_id ASC, id ASC
                    """,
                    thread_ids,
                ).fetchall()
            ]
    finally:
        conn.close()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for thread in threads:
        nodes.append(
            {
                "id": f"thread-{thread['id']}",
                "type": "thread",
                "thread_id": thread["id"],
                "key": thread.get("source_event_key"),
                "label": thread.get("title"),
                "status": thread.get("status"),
                "summary": thread.get("current_summary") or "",
            }
        )
    previous_by_thread: dict[int, str] = {}
    for step in steps:
        thread_id = int(step["thread_id"])
        node_id = f"step-{step['id']}"
        nodes.append(
            {
                "id": node_id,
                "type": "step",
                "thread_id": thread_id,
                "label": step.get("summary"),
                "step_type": step.get("step_type"),
                "cause": step.get("cause"),
                "reflection": step.get("reflection"),
                "created_at": step.get("created_at"),
                "occurred_at": step.get("occurred_at"),
            }
        )
        source = previous_by_thread.get(thread_id) or f"thread-{thread_id}"
        edges.append(
            {
                "id": f"edge-{source}-{node_id}",
                "source": source,
                "target": node_id,
                "label": step.get("cause") or step.get("step_type"),
                "step_type": step.get("step_type"),
            }
        )
        previous_by_thread[thread_id] = node_id

    return {
        "memory_path": str(path),
        "exists": True,
        "threads": threads,
        "steps": steps,
        "nodes": nodes,
        "edges": edges,
    }


@app.get("/api/instances/{instance_id}/event_graph")
def api_instance_event_graph(instance_id: str) -> dict[str, Any]:
    try:
        path = instance_store.memory_db_path(instance_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _event_graph_from_path(path)


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


@app.post("/api/group_arbitrate")
async def api_group_arbitrate(body: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(arbitrator.arbitrate, body)


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
