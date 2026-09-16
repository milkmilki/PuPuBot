"""Self-contained HTML export for event-thread and fact graphs."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .memory import event_graph_payload, get_data_dir


_TEMPLATE_PATH = Path(__file__).with_name("templates") / "event_graph.html"
_TEMPLATE_SLOT = re.compile(r"__(TITLE|DATA_JSON)__")


def _safe_filename_part(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", str(value or "").strip())
    return text.strip("-")[:80] or "default"


def _json_script_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


@lru_cache(maxsize=1)
def _load_event_graph_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def write_event_graph_html(session_id: str, *, output_dir: str | Path | None = None) -> Path:
    """Write a local, standalone event graph HTML page and return its path."""
    payload = event_graph_payload(session_id)
    generated_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        **payload,
        "session_id": session_id,
        "generated_at": generated_at,
    }

    base_dir = Path(output_dir) if output_dir is not None else Path(get_data_dir()) / "event_graph"
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = base_dir / f"event-graph-{_safe_filename_part(session_id)}-{stamp}.html"
    path.write_text(_build_event_graph_html(payload), encoding="utf-8")
    return path


def format_event_graph_url_report(session_id: str) -> str:
    path = write_event_graph_html(session_id)
    url = path.resolve().as_uri()
    return (
        "事件图谱网页已生成：\n"
        f"{url}\n"
        f"本地路径：{path.resolve()}\n"
        "页面是自包含 HTML，打开后可以在 events / facts 两种视图中缩放、拖拽、搜索和查看详情。"
    )


def _build_event_graph_html(payload: dict[str, Any]) -> str:
    replacements = {
        "TITLE": html.escape(f"PuPu 事件图谱 - {payload.get('session_id') or 'default'}"),
        "DATA_JSON": _json_script_payload(payload),
    }
    return _TEMPLATE_SLOT.sub(
        lambda match: replacements[match.group(1)],
        _load_event_graph_template(),
    )
