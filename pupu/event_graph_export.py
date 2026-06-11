"""Self-contained HTML export for event-thread graphs."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory import event_graph_payload, get_data_dir


def _safe_filename_part(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", str(value or "").strip())
    return text.strip("-")[:80] or "default"


def _json_script_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


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
        "页面是自包含 HTML，打开后可以缩放、拖拽、搜索和查看事件线进展。"
    )


def _build_event_graph_html(payload: dict[str, Any]) -> str:
    title = f"PuPu 事件图谱 - {payload.get('session_id') or 'default'}"
    data_json = _json_script_payload(payload)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7fb;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #687386;
      --line: #d7dde8;
      --thread: #2563eb;
      --user: #059669;
      --instance: #db2777;
      --time: #b45309;
      --system: #64748b;
      --selected: #111827;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 22px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0;
      font-size: 19px;
      font-weight: 700;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    main {{
      height: calc(100vh - 64px);
      min-height: 620px;
      display: grid;
      grid-template-columns: 320px minmax(420px, 1fr) 360px;
    }}
    aside, section {{
      min-width: 0;
      min-height: 0;
    }}
    .sidebar, .detail {{
      background: var(--panel);
      border-right: 1px solid var(--line);
      overflow: auto;
    }}
    .detail {{
      border-right: 0;
      border-left: 1px solid var(--line);
    }}
    .tools {{
      padding: 16px;
      border-bottom: 1px solid var(--line);
      display: grid;
      gap: 10px;
    }}
    input, select, button {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    input, select {{ padding: 0 10px; }}
    button {{
      cursor: pointer;
      font-weight: 650;
    }}
    .thread-list {{
      display: grid;
      gap: 8px;
      padding: 12px;
    }}
    .thread-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      cursor: pointer;
    }}
    .thread-item:hover, .thread-item.active {{
      border-color: #2563eb;
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.12);
    }}
    .thread-title {{
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      margin-bottom: 6px;
    }}
    .thread-summary, .small {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 0 8px;
      background: #edf2f7;
      color: #334155;
      font-size: 12px;
      font-weight: 650;
    }}
    .canvas-wrap {{
      position: relative;
      overflow: hidden;
      background: #f8fafc;
    }}
    #graph {{
      width: 100%;
      height: 100%;
      display: block;
      touch-action: none;
      user-select: none;
      cursor: grab;
    }}
    #graph.is-panning {{
      cursor: grabbing;
    }}
    .graph-help {{
      position: absolute;
      left: 16px;
      bottom: 16px;
      max-width: 420px;
      color: var(--muted);
      background: rgba(255,255,255,0.92);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      pointer-events: none;
    }}
    .empty {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 24px;
    }}
    .empty[hidden] {{
      display: none;
    }}
    .link {{
      stroke: #aab4c4;
      stroke-width: 1.6;
      opacity: 0.72;
    }}
    .link.selected {{
      stroke: var(--selected);
      stroke-width: 2.4;
      opacity: 0.95;
    }}
    .node circle {{
      stroke: #fff;
      stroke-width: 2.2;
      filter: drop-shadow(0 3px 5px rgba(15, 23, 42, 0.18));
    }}
    .node text {{
      pointer-events: none;
      font-size: 12px;
      fill: #1f2937;
      paint-order: stroke;
      stroke: rgba(255,255,255,0.88);
      stroke-width: 4px;
      stroke-linejoin: round;
    }}
    .node.selected circle {{
      stroke: #111827;
      stroke-width: 3px;
    }}
    .detail-inner {{
      padding: 18px;
    }}
    .detail h2 {{
      font-size: 18px;
      margin: 0 0 8px;
      line-height: 1.35;
    }}
    .detail-block {{
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }}
    .timeline {{
      display: grid;
      gap: 12px;
      margin-top: 12px;
    }}
    .step {{
      position: relative;
      border-left: 3px solid #cbd5e1;
      padding-left: 12px;
    }}
    .step.user {{ border-left-color: var(--user); }}
    .step.instance {{ border-left-color: var(--instance); }}
    .step.time {{ border-left-color: var(--time); }}
    .step.system {{ border-left-color: var(--system); }}
    .step-head {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }}
    .step-summary {{
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
    }}
    .legend {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }}
    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: currentColor;
      flex: 0 0 auto;
    }}
    @media (max-width: 1100px) {{
      main {{
        height: auto;
        min-height: 0;
        grid-template-columns: 1fr;
      }}
      .sidebar, .detail {{ border: 0; }}
      .canvas-wrap {{ height: 620px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>PuPu 事件图谱</h1>
    <div class="meta">session: <strong id="session-id"></strong> · generated: <span id="generated-at"></span></div>
  </header>
  <main>
    <aside class="sidebar">
      <div class="tools">
        <input id="search" placeholder="搜索标题、状态、触发原因..." />
        <select id="status-filter">
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="scheduled">scheduled</option>
          <option value="done">done</option>
          <option value="missed">missed</option>
          <option value="dropped">dropped</option>
        </select>
        <button id="fit">适配视图</button>
        <div class="legend">
          <span style="color: var(--thread)"><i class="dot"></i>事件线</span>
          <span style="color: var(--user)"><i class="dot"></i>用户触发</span>
          <span style="color: var(--instance)"><i class="dot"></i>实例触发</span>
          <span style="color: var(--time)"><i class="dot"></i>时间推测</span>
          <span style="color: var(--system)"><i class="dot"></i>系统整理</span>
        </div>
      </div>
      <div id="thread-list" class="thread-list"></div>
    </aside>
    <section class="canvas-wrap">
      <svg id="graph" role="img" aria-label="事件知识图谱"></svg>
      <div id="empty" class="empty" hidden>还没有事件线。等 batch review 写入事件图谱后再打开这里。</div>
      <div class="graph-help">滚轮缩放，拖动画布平移，拖动节点调整布局；点击节点或左侧事件线查看完整进展。</div>
    </section>
    <aside class="detail">
      <div id="detail" class="detail-inner"></div>
    </aside>
  </main>
  <script>
    const EVENT_GRAPH_DATA = {data_json};
    const state = {{
      selectedThreadId: null,
      query: "",
      status: "",
      scale: 1,
      tx: 0,
      ty: 0,
      draggingNode: null,
      panning: false,
      lastPointer: null,
      viewport: null,
      visibleNodes: [],
      visibleEdges: [],
      edgeEls: new Map(),
      nodeEls: new Map(),
      activePointers: new Map(),
      pinch: null,
      activeNodePointerId: null,
      suppressClick: false,
    }};
    const colors = {{
      thread: "#2563eb",
      user: "#059669",
      instance: "#db2777",
      time: "#b45309",
      system: "#64748b",
    }};
    const svg = document.getElementById("graph");
    const empty = document.getElementById("empty");
    const listEl = document.getElementById("thread-list");
    const detailEl = document.getElementById("detail");
    document.getElementById("session-id").textContent = EVENT_GRAPH_DATA.session_id || "";
    document.getElementById("generated-at").textContent = EVENT_GRAPH_DATA.generated_at || "";

    const threads = EVENT_GRAPH_DATA.threads || [];
    const steps = EVENT_GRAPH_DATA.steps || [];
    const allNodes = (EVENT_GRAPH_DATA.nodes || []).map((node, index) => ({{
      ...node,
      x: 260 + (index % 7) * 64,
      y: 170 + Math.floor(index / 7) * 58,
      vx: 0,
      vy: 0,
    }}));
    const allEdges = (EVENT_GRAPH_DATA.edges || []).map((edge) => ({{ ...edge }}));
    const byNodeId = new Map(allNodes.map((node) => [node.id, node]));
    const stepsByThread = new Map();
    for (const step of steps) {{
      const list = stepsByThread.get(String(step.thread_id)) || [];
      list.push(step);
      stepsByThread.set(String(step.thread_id), list);
    }}
    if (threads.length && !state.selectedThreadId) state.selectedThreadId = String(threads[0].id);

    function escapeHtml(value) {{
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}
    function shortText(value, max = 32) {{
      const text = String(value || "").replace(/\\s+/g, " ").trim();
      return text.length <= max ? text : text.slice(0, max - 1) + "…";
    }}
    function stepLabel(type) {{
      return {{ user: "用户", instance: "实例", time: "时间", system: "系统" }}[type] || type || "进展";
    }}
    function threadText(thread) {{
      return [
        thread.title,
        thread.source_event_key,
        thread.kind,
        thread.status,
        thread.current_summary,
        thread.current_cause,
        thread.followup_hint,
        thread.search_text,
        thread.merge_hint,
      ].join(" ").toLowerCase();
    }}
    function visibleThreads() {{
      const query = state.query.toLowerCase().trim();
      return threads.filter((thread) => {{
        if (state.status && String(thread.status || "") !== state.status) return false;
        if (query && !threadText(thread).includes(query)) return false;
        return true;
      }});
    }}
    function visibleThreadIdSet() {{
      return new Set(visibleThreads().map((thread) => String(thread.id)));
    }}
    function renderList() {{
      const shown = visibleThreads();
      listEl.innerHTML = shown.map((thread) => {{
        const active = String(thread.id) === String(state.selectedThreadId) ? " active" : "";
        return `<div class="thread-item${{active}}" data-thread="${{escapeHtml(thread.id)}}">`
          + `<div class="thread-title">${{escapeHtml(thread.title || "未命名事件线")}}</div>`
          + `<div class="thread-summary">${{escapeHtml(shortText(thread.current_summary || thread.details || "", 88))}}</div>`
          + `<div class="badges">`
          + `<span class="badge">${{escapeHtml(thread.status || "active")}}</span>`
          + `<span class="badge">key=${{escapeHtml(thread.source_event_key || "")}}</span>`
          + `<span class="badge">steps=${{(stepsByThread.get(String(thread.id)) || []).length}}</span>`
          + `</div></div>`;
      }}).join("");
      for (const item of listEl.querySelectorAll(".thread-item")) {{
        item.addEventListener("click", () => selectThread(item.dataset.thread));
      }}
    }}
    function selectThread(threadId) {{
      state.selectedThreadId = String(threadId || "");
      renderList();
      renderDetail();
      draw();
    }}
    function filteredGraph() {{
      const visibleIds = visibleThreadIdSet();
      const nodes = allNodes.filter((node) => {{
        if (node.type === "thread") return visibleIds.has(String(node.thread_id));
        return visibleIds.has(String(node.thread_id));
      }});
      const nodeIds = new Set(nodes.map((node) => node.id));
      const edges = allEdges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
      return {{ nodes, edges }};
    }}
    function nodeRadius(node) {{
      if (node.type === "thread") return 24;
      return node.step_type === "time" ? 14 : 16;
    }}
    function nodeColor(node) {{
      if (node.type === "thread") return colors.thread;
      return colors[node.step_type] || colors.system;
    }}
    function simulate(nodes, edges, iterations = 90) {{
      if (!nodes.length) return;
      const bounds = svg.getBoundingClientRect();
      const cx = Math.max(300, bounds.width || 900) / 2;
      const cy = Math.max(300, bounds.height || 650) / 2;
      for (let tick = 0; tick < iterations; tick++) {{
        for (const node of nodes) {{
          const targetX = node.type === "thread" ? cx - 140 : cx + 80;
          const targetY = cy + (Number(node.thread_id || 0) % 9 - 4) * 28;
          node.vx += (targetX - node.x) * 0.002;
          node.vy += (targetY - node.y) * 0.002;
        }}
        for (let i = 0; i < nodes.length; i++) {{
          for (let j = i + 1; j < nodes.length; j++) {{
            const a = nodes[i], b = nodes[j];
            let dx = a.x - b.x;
            let dy = a.y - b.y;
            let dist2 = dx * dx + dy * dy || 0.01;
            const force = Math.min(7, 900 / dist2);
            const dist = Math.sqrt(dist2);
            dx /= dist; dy /= dist;
            a.vx += dx * force;
            a.vy += dy * force;
            b.vx -= dx * force;
            b.vy -= dy * force;
          }}
        }}
        for (const edge of edges) {{
          const source = byNodeId.get(edge.source);
          const target = byNodeId.get(edge.target);
          if (!source || !target) continue;
          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const desired = source.type === "thread" ? 120 : 92;
          const force = (dist - desired) * 0.018;
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          source.vx += fx;
          source.vy += fy;
          target.vx -= fx;
          target.vy -= fy;
        }}
        for (const node of nodes) {{
          if (state.draggingNode === node) continue;
          node.vx *= 0.76;
          node.vy *= 0.76;
          node.x += node.vx;
          node.y += node.vy;
        }}
      }}
    }}
    function graphTransform() {{
      return `translate(${{state.tx}},${{state.ty}}) scale(${{state.scale}})`;
    }}
    function applyTransform() {{
      if (state.viewport) state.viewport.setAttribute("transform", graphTransform());
    }}
    function updateEdgePositions() {{
      for (const edge of state.visibleEdges) {{
        const line = state.edgeEls.get(edge.id || `${{edge.source}}->${{edge.target}}`);
        const source = byNodeId.get(edge.source);
        const target = byNodeId.get(edge.target);
        if (!line || !source || !target) continue;
        line.setAttribute("x1", source.x);
        line.setAttribute("y1", source.y);
        line.setAttribute("x2", target.x);
        line.setAttribute("y2", target.y);
      }}
    }}
    function updateNodePosition(node) {{
      const group = state.nodeEls.get(node.id);
      if (group) group.setAttribute("transform", `translate(${{node.x}},${{node.y}})`);
    }}
    function updateGraphPositions() {{
      for (const node of state.visibleNodes) updateNodePosition(node);
      updateEdgePositions();
    }}
    function setPanning(active) {{
      state.panning = active;
      svg.classList.toggle("is-panning", active);
    }}
    function moveDraggedNode(ev) {{
      const node = state.draggingNode;
      if (!node || state.activeNodePointerId !== ev.pointerId) return false;
      ev.preventDefault();
      const pt = toGraphPoint(ev);
      if (Math.abs(node.x - pt.x) > 1 || Math.abs(node.y - pt.y) > 1) state.suppressClick = true;
      node.x = pt.x;
      node.y = pt.y;
      updateNodePosition(node);
      updateEdgePositions();
      return true;
    }}
    function endDraggedNode(ev) {{
      const node = state.draggingNode;
      if (!node || state.activeNodePointerId !== ev.pointerId) return false;
      state.draggingNode = null;
      state.activeNodePointerId = null;
      const group = state.nodeEls.get(node.id);
      if (group) {{
        try {{ group.releasePointerCapture(ev.pointerId); }} catch (_e) {{}}
        group.style.cursor = "grab";
      }}
      return true;
    }}
    function draw() {{
      const {{ nodes, edges }} = filteredGraph();
      state.visibleNodes = nodes;
      state.visibleEdges = edges;
      state.edgeEls = new Map();
      state.nodeEls = new Map();
      empty.hidden = nodes.length > 0;
      simulate(nodes, edges, 28);
      svg.innerHTML = "";
      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      state.viewport = g;
      applyTransform();
      svg.appendChild(g);
      for (const edge of edges) {{
        const source = byNodeId.get(edge.source);
        const target = byNodeId.get(edge.target);
        if (!source || !target) continue;
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        const edgeId = edge.id || `${{edge.source}}->${{edge.target}}`;
        line.setAttribute("x1", source.x);
        line.setAttribute("y1", source.y);
        line.setAttribute("x2", target.x);
        line.setAttribute("y2", target.y);
        line.setAttribute("class", "link" + (String(source.thread_id) === String(state.selectedThreadId) ? " selected" : ""));
        state.edgeEls.set(edgeId, line);
        g.appendChild(line);
      }}
      for (const node of nodes) {{
        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
        const selected = String(node.thread_id) === String(state.selectedThreadId);
        group.setAttribute("class", "node" + (selected ? " selected" : ""));
        group.setAttribute("transform", `translate(${{node.x}},${{node.y}})`);
        group.style.cursor = "grab";
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("r", nodeRadius(node));
        circle.setAttribute("fill", nodeColor(node));
        group.appendChild(circle);
        const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
        text.setAttribute("x", nodeRadius(node) + 7);
        text.setAttribute("y", 4);
        text.textContent = shortText(node.label || node.summary || node.key || "", node.type === "thread" ? 26 : 30);
        group.appendChild(text);
        group.addEventListener("pointerdown", (ev) => {{
          ev.preventDefault();
          ev.stopPropagation();
          state.draggingNode = node;
          state.activeNodePointerId = ev.pointerId;
          state.suppressClick = false;
          try {{ group.setPointerCapture(ev.pointerId); }} catch (_e) {{}}
          group.style.cursor = "grabbing";
        }});
        group.addEventListener("pointermove", (ev) => {{
          moveDraggedNode(ev);
        }});
        const endNodeDrag = (ev) => {{
          endDraggedNode(ev);
        }};
        group.addEventListener("pointerup", endNodeDrag);
        group.addEventListener("pointercancel", endNodeDrag);
        group.addEventListener("lostpointercapture", () => {{
          if (state.draggingNode === node) state.draggingNode = null;
          state.activeNodePointerId = null;
          group.style.cursor = "grab";
        }});
        group.addEventListener("click", () => {{
          if (state.suppressClick) {{
            state.suppressClick = false;
            return;
          }}
          selectThread(node.thread_id);
        }});
        state.nodeEls.set(node.id, group);
        g.appendChild(group);
      }}
    }}
    function toGraphPoint(ev) {{
      const rect = svg.getBoundingClientRect();
      return {{
        x: (ev.clientX - rect.left - state.tx) / state.scale,
        y: (ev.clientY - rect.top - state.ty) / state.scale,
      }};
    }}
    function fitView() {{
      const {{ nodes }} = filteredGraph();
      if (!nodes.length) return;
      const rect = svg.getBoundingClientRect();
      const minX = Math.min(...nodes.map((n) => n.x - nodeRadius(n)));
      const maxX = Math.max(...nodes.map((n) => n.x + nodeRadius(n)));
      const minY = Math.min(...nodes.map((n) => n.y - nodeRadius(n)));
      const maxY = Math.max(...nodes.map((n) => n.y + nodeRadius(n)));
      const graphW = Math.max(1, maxX - minX);
      const graphH = Math.max(1, maxY - minY);
      state.scale = Math.min(1.6, Math.max(0.35, Math.min((rect.width - 80) / graphW, (rect.height - 80) / graphH)));
      state.tx = rect.width / 2 - ((minX + maxX) / 2) * state.scale;
      state.ty = rect.height / 2 - ((minY + maxY) / 2) * state.scale;
      applyTransform();
    }}
    function renderDetail() {{
      const thread = threads.find((item) => String(item.id) === String(state.selectedThreadId));
      if (!thread) {{
        detailEl.innerHTML = `<h2>没有选中事件线</h2><p class="small">点击左侧事件线或图谱节点查看详情。</p>`;
        return;
      }}
      const threadSteps = stepsByThread.get(String(thread.id)) || [];
      detailEl.innerHTML = `
        <h2>${{escapeHtml(thread.title || "未命名事件线")}}</h2>
        <div class="small">key=${{escapeHtml(thread.source_event_key || "")}}</div>
        <div class="badges">
          <span class="badge">${{escapeHtml(thread.status || "active")}}</span>
          <span class="badge">${{escapeHtml(thread.kind || "event")}}</span>
          <span class="badge">confidence=${{Number(thread.confidence || 0).toFixed(2)}}</span>
        </div>
        <div class="detail-block">
          <div class="small">当前状态</div>
          <p>${{escapeHtml(thread.current_summary || thread.details || "暂无当前状态")}}</p>
          ${{thread.followup_hint ? `<div class="small">跟进提示</div><p>${{escapeHtml(thread.followup_hint)}}</p>` : ""}}
        </div>
        <div class="detail-block">
          <div class="small">进展时间线 · ${{threadSteps.length}} 条</div>
          <div class="timeline">
            ${{threadSteps.map((step, idx) => `
              <div class="step ${{escapeHtml(step.step_type || "user")}}">
                <div class="step-head">
                  <strong>#${{idx + 1}} · ${{escapeHtml(stepLabel(step.step_type))}}</strong>
                  <span>${{escapeHtml(step.occurred_at || step.created_at || "")}}</span>
                </div>
                <div class="step-summary">${{escapeHtml(step.summary || "")}}</div>
                ${{step.cause ? `<div class="small">触发：${{escapeHtml(step.cause)}}</div>` : ""}}
                ${{step.reflection ? `<div class="small">反思：${{escapeHtml(step.reflection)}}</div>` : ""}}
              </div>
            `).join("")}}
          </div>
        </div>`;
    }}
    document.getElementById("search").addEventListener("input", (ev) => {{
      state.query = ev.target.value || "";
      if (!visibleThreadIdSet().has(String(state.selectedThreadId))) {{
        const first = visibleThreads()[0];
        state.selectedThreadId = first ? String(first.id) : null;
      }}
      renderList();
      renderDetail();
      draw();
    }});
    document.getElementById("status-filter").addEventListener("change", (ev) => {{
      state.status = ev.target.value || "";
      const first = visibleThreads()[0];
      if (first && !visibleThreadIdSet().has(String(state.selectedThreadId))) state.selectedThreadId = String(first.id);
      renderList();
      renderDetail();
      draw();
    }});
    document.getElementById("fit").addEventListener("click", fitView);
    svg.addEventListener("wheel", (ev) => {{
      ev.preventDefault();
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const oldScale = state.scale;
      const factor = ev.deltaY < 0 ? 1.12 : 0.89;
      state.scale = Math.min(3.2, Math.max(0.25, state.scale * factor));
      state.tx = mx - ((mx - state.tx) / oldScale) * state.scale;
      state.ty = my - ((my - state.ty) / oldScale) * state.scale;
      applyTransform();
    }}, {{ passive: false }});
    function updatePinch() {{
      const points = Array.from(state.activePointers.values());
      if (points.length < 2) {{
        state.pinch = null;
        return;
      }}
      const a = points[0], b = points[1];
      const cx = (a.x + b.x) / 2;
      const cy = (a.y + b.y) / 2;
      const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      if (!state.pinch) {{
        state.pinch = {{
          cx,
          cy,
          distance,
          scale: state.scale,
          tx: state.tx,
          ty: state.ty,
        }};
        return;
      }}
      const nextScale = Math.min(3.2, Math.max(0.25, state.pinch.scale * (distance / state.pinch.distance)));
      const graphX = (state.pinch.cx - state.pinch.tx) / state.pinch.scale;
      const graphY = (state.pinch.cy - state.pinch.ty) / state.pinch.scale;
      state.scale = nextScale;
      state.tx = cx - graphX * nextScale;
      state.ty = cy - graphY * nextScale;
      applyTransform();
    }}
    svg.addEventListener("pointerdown", (ev) => {{
      ev.preventDefault();
      state.activePointers.set(ev.pointerId, {{ x: ev.clientX, y: ev.clientY }});
      if (state.activePointers.size >= 2) {{
        setPanning(false);
        state.lastPointer = null;
        updatePinch();
      }} else {{
        setPanning(true);
        state.lastPointer = {{ x: ev.clientX, y: ev.clientY }};
      }}
      try {{ svg.setPointerCapture(ev.pointerId); }} catch (_e) {{}}
    }});
    svg.addEventListener("pointermove", (ev) => {{
      if (moveDraggedNode(ev)) return;
      if (!state.activePointers.has(ev.pointerId)) return;
      ev.preventDefault();
      state.activePointers.set(ev.pointerId, {{ x: ev.clientX, y: ev.clientY }});
      if (state.activePointers.size >= 2) {{
        updatePinch();
        return;
      }}
      if (!state.panning || !state.lastPointer || state.draggingNode) return;
      state.tx += ev.clientX - state.lastPointer.x;
      state.ty += ev.clientY - state.lastPointer.y;
      state.lastPointer = {{ x: ev.clientX, y: ev.clientY }};
      applyTransform();
    }});
    function endCanvasPointer(ev) {{
      if (endDraggedNode(ev)) return;
      state.activePointers.delete(ev.pointerId);
      if (state.activePointers.size >= 2) {{
        updatePinch();
      }} else if (state.activePointers.size === 1) {{
        const only = Array.from(state.activePointers.values())[0];
        state.pinch = null;
        setPanning(true);
        state.lastPointer = {{ x: only.x, y: only.y }};
      }} else {{
        state.pinch = null;
        setPanning(false);
        state.lastPointer = null;
      }}
      try {{ svg.releasePointerCapture(ev.pointerId); }} catch (_e) {{}}
    }}
    svg.addEventListener("pointerup", endCanvasPointer);
    svg.addEventListener("pointercancel", endCanvasPointer);
    svg.addEventListener("lostpointercapture", (ev) => {{
      state.activePointers.delete(ev.pointerId);
      if (!state.activePointers.size) {{
        state.pinch = null;
        setPanning(false);
        state.lastPointer = null;
      }}
    }});
    svg.addEventListener("dblclick", (ev) => {{
      ev.preventDefault();
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const oldScale = state.scale;
      state.scale = Math.min(3.2, state.scale * 1.35);
      state.tx = mx - ((mx - state.tx) / oldScale) * state.scale;
      state.ty = my - ((my - state.ty) / oldScale) * state.scale;
      applyTransform();
    }});
    window.addEventListener("resize", () => {{
      updateGraphPositions();
      applyTransform();
    }});
    renderList();
    renderDetail();
    draw();
    setTimeout(fitView, 80);
  </script>
</body>
</html>
"""
