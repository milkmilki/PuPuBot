"""Self-contained HTML export for event-thread and fact graphs."""

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
        "页面是自包含 HTML，打开后可以在 events / facts 两种视图中缩放、拖拽、搜索和查看详情。"
    )


def _build_event_graph_html(payload: dict[str, Any]) -> str:
    title = f"PuPu 事件图谱 - {payload.get('session_id') or 'default'}"
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7fb;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #687386;
      --line: #d7dde8;
      --thread: #2563eb;
      --person: #7c3aed;
      --fact: #16a34a;
      --relationship: #db2777;
      --user: #059669;
      --instance: #db2777;
      --time: #b45309;
      --system: #64748b;
      --selected: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }
    header {
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 22px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
    }
    h1 {
      margin: 0;
      font-size: 19px;
      font-weight: 700;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    main {
      height: calc(100vh - 64px);
      min-height: 620px;
      display: grid;
      grid-template-columns: 320px minmax(420px, 1fr) 360px;
    }
    aside, section {
      min-width: 0;
      min-height: 0;
    }
    .sidebar, .detail {
      background: var(--panel);
      border-right: 1px solid var(--line);
      overflow: auto;
    }
    .detail {
      border-right: 0;
      border-left: 1px solid var(--line);
    }
    .tools {
      padding: 16px;
      border-bottom: 1px solid var(--line);
      display: grid;
      gap: 10px;
    }
    input, select, button {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    input, select { padding: 0 10px; }
    button {
      cursor: pointer;
      font-weight: 650;
    }
    select[hidden] { display: none; }
    .view-choice {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      padding: 4px;
      background: #eef2f7;
      border: 1px solid var(--line);
      border-radius: 10px;
    }
    .view-choice button {
      min-height: 34px;
      border: 0;
      background: transparent;
      border-radius: 7px;
      color: var(--muted);
    }
    .view-choice button.active {
      background: #fff;
      color: var(--ink);
      box-shadow: 0 1px 4px rgba(15, 23, 42, 0.12);
    }
    .item-list {
      display: grid;
      gap: 8px;
      padding: 12px;
    }
    .item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      cursor: pointer;
      text-align: left;
    }
    .item:hover, .item.active {
      border-color: #2563eb;
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.12);
    }
    .item-title {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      margin-bottom: 6px;
    }
    .item-summary, .small {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .badges {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 0 8px;
      background: #edf2f7;
      color: #334155;
      font-size: 12px;
      font-weight: 650;
    }
    .canvas-wrap {
      position: relative;
      overflow: hidden;
      background: #f8fafc;
    }
    #graph {
      width: 100%;
      height: 100%;
      display: block;
      touch-action: none;
      user-select: none;
      cursor: grab;
    }
    #graph.is-panning {
      cursor: grabbing;
    }
    .graph-help {
      position: absolute;
      left: 16px;
      bottom: 16px;
      max-width: 460px;
      color: var(--muted);
      background: rgba(255,255,255,0.92);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      pointer-events: none;
    }
    .empty {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 24px;
    }
    .empty[hidden] {
      display: none;
    }
    .link {
      stroke: #aab4c4;
      stroke-width: 1.6;
      opacity: 0.72;
    }
    .link.selected {
      stroke: var(--selected);
      stroke-width: 2.4;
      opacity: 0.95;
    }
    .link.fact {
      stroke: var(--fact);
      stroke-dasharray: 4 4;
    }
    .link.relationship {
      stroke: var(--relationship);
      stroke-width: 2.2;
    }
    .node circle {
      stroke: #fff;
      stroke-width: 2.2;
      filter: drop-shadow(0 3px 5px rgba(15, 23, 42, 0.18));
    }
    .node text {
      pointer-events: none;
      font-size: 12px;
      fill: #1f2937;
      paint-order: stroke;
      stroke: rgba(255,255,255,0.88);
      stroke-width: 4px;
      stroke-linejoin: round;
    }
    .node.selected circle {
      stroke: #111827;
      stroke-width: 3px;
    }
    .detail-inner {
      padding: 18px;
    }
    .detail h2 {
      font-size: 18px;
      margin: 0 0 8px;
      line-height: 1.35;
    }
    .detail-block {
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }
    .timeline {
      display: grid;
      gap: 12px;
      margin-top: 12px;
    }
    .step {
      position: relative;
      border-left: 3px solid #cbd5e1;
      padding-left: 12px;
    }
    .step.user { border-left-color: var(--user); }
    .step.instance { border-left-color: var(--instance); }
    .step.time { border-left-color: var(--time); }
    .step.system { border-left-color: var(--system); }
    .step-head {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }
    .step-summary {
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
    }
    .legend {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .legend span {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: currentColor;
      flex: 0 0 auto;
    }
    @media (max-width: 1100px) {
      main {
        height: auto;
        min-height: 0;
        grid-template-columns: 1fr;
      }
      .sidebar, .detail { border: 0; }
      .canvas-wrap { height: 620px; }
    }
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
        <div class="view-choice" role="tablist" aria-label="图谱视图">
          <button type="button" id="view-events" data-view="events">events 视图</button>
          <button type="button" id="view-facts" data-view="facts">facts 视图</button>
        </div>
        <input id="search" placeholder="搜索事件线..." />
        <select id="status-filter">
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="scheduled">scheduled</option>
          <option value="done">done</option>
          <option value="missed">missed</option>
          <option value="cancelled">cancelled</option>
        </select>
        <button id="fit">适配视图</button>
        <div id="legend" class="legend"></div>
      </div>
      <div id="item-list" class="item-list"></div>
    </aside>
    <section class="canvas-wrap">
      <svg id="graph" role="img" aria-label="PuPu 记忆图谱"></svg>
      <div id="empty" class="empty" hidden></div>
      <div class="graph-help">滚轮缩放，拖动画布平移，拖动节点调整位置；点击节点或左侧列表查看详情。</div>
    </section>
    <aside class="detail">
      <div id="detail" class="detail-inner"></div>
    </aside>
  </main>
  <script>
    const EVENT_GRAPH_DATA = __DATA_JSON__;
    const state = {
      view: "events",
      selectedThreadId: null,
      selectedFactId: null,
      selectedPersonId: null,
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
      physicsFrame: 0,
      physicsRunning: false,
      factsLayoutKey: "",
      activePointers: new Map(),
      pinch: null,
      activeNodePointerId: null,
      suppressClick: false,
    };
    const colors = {
      thread: "#2563eb",
      person: "#7c3aed",
      fact: "#16a34a",
      relationship: "#db2777",
      user: "#059669",
      instance: "#db2777",
      time: "#b45309",
      system: "#64748b",
    };
    const svg = document.getElementById("graph");
    const empty = document.getElementById("empty");
    const listEl = document.getElementById("item-list");
    const detailEl = document.getElementById("detail");
    const searchEl = document.getElementById("search");
    const statusEl = document.getElementById("status-filter");
    const legendEl = document.getElementById("legend");
    document.getElementById("session-id").textContent = EVENT_GRAPH_DATA.session_id || "";
    document.getElementById("generated-at").textContent = EVENT_GRAPH_DATA.generated_at || "";

    const threads = EVENT_GRAPH_DATA.threads || [];
    const steps = EVENT_GRAPH_DATA.steps || [];
    const facts = EVENT_GRAPH_DATA.facts || [];
    const allNodes = (EVENT_GRAPH_DATA.nodes || []).map((node) => ({
      ...node,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null,
    }));
    const allEdges = (EVENT_GRAPH_DATA.edges || []).map((edge) => ({ ...edge }));
    const byNodeId = new Map(allNodes.map((node) => [node.id, node]));
    const factsById = new Map(facts.map((fact) => [String(fact.id), fact]));
    const stepsByThread = new Map();
    for (const step of steps) {
      const list = stepsByThread.get(String(step.thread_id)) || [];
      list.push(step);
      stepsByThread.set(String(step.thread_id), list);
    }
    if (threads.length) state.selectedThreadId = String(threads[0].id);
    if (facts.length) state.selectedFactId = String(facts[0].id);

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }
    function shortText(value, max = 32) {
      const text = String(value || "").replace(/\\s+/g, " ").trim();
      return text.length <= max ? text : text.slice(0, max - 1) + "…";
    }
    function stepLabel(type) {
      return { user: "用户", instance: "实例", time: "时间", system: "系统" }[type] || type || "进展";
    }
    function personLabel(personKey) {
      const node = byNodeId.get(`person-${personKey || ""}`);
      return node?.label || personKey || "";
    }
    function threadSteps(threadId) {
      return stepsByThread.get(String(threadId)) || [];
    }
    function threadText(thread) {
      return [
        thread.title,
        thread.thread_key,
        thread.key,
        thread.kind,
        thread.status,
        thread.people_label,
        thread.current_summary,
        thread.current_cause,
        thread.followup_hint,
        thread.search_text,
        thread.merge_hint,
        ...threadSteps(thread.id).flatMap((step) => [step.summary, step.cause, step.people_label]),
      ].join(" ").toLowerCase();
    }
    function factText(fact) {
      return [
        fact.scope,
        fact.fact_key,
        fact.fact_value,
        fact.subject_person_key,
        fact.object_person_key,
        fact.subject_display_name,
        fact.object_display_name,
        personLabel(fact.subject_person_key),
        personLabel(fact.object_person_key),
      ].join(" ").toLowerCase();
    }
    function visibleThreads() {
      const query = state.query.toLowerCase().trim();
      return threads.filter((thread) => {
        if (state.status && String(thread.status || "") !== state.status) return false;
        if (query && !threadText(thread).includes(query)) return false;
        return true;
      });
    }
    function visibleFacts() {
      const query = state.query.toLowerCase().trim();
      return facts.filter((fact) => !query || factText(fact).includes(query));
    }
    function visibleThreadIdSet() {
      return new Set(visibleThreads().map((thread) => String(thread.id)));
    }
    function visibleFactNodeIdSet() {
      return new Set(visibleFacts().map((fact) => `fact-${fact.id}`));
    }
    function updateControls() {
      document.querySelectorAll(".view-choice button").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.view === state.view);
      });
      statusEl.hidden = state.view !== "events";
      searchEl.placeholder = state.view === "events"
        ? "搜索事件线、状态、触发原因..."
        : "搜索人物、facts、关系...";
      legendEl.innerHTML = state.view === "events"
        ? `<span style="color: var(--person)"><i class="dot"></i>人物</span>
           <span style="color: var(--thread)"><i class="dot"></i>事件线</span>
           <span style="color: var(--user)"><i class="dot"></i>用户触发</span>
           <span style="color: var(--instance)"><i class="dot"></i>实例触发</span>
           <span style="color: var(--time)"><i class="dot"></i>时间推测</span>
           <span style="color: var(--system)"><i class="dot"></i>系统记录</span>`
        : `<span style="color: var(--person)"><i class="dot"></i>人物</span>
           <span style="color: var(--fact)"><i class="dot"></i>人物 fact</span>
           <span style="color: var(--relationship)"><i class="dot"></i>关系 fact</span>`;
    }
    function ensureSelection() {
      if (state.view === "events") {
        if (!visibleThreadIdSet().has(String(state.selectedThreadId))) {
          const first = visibleThreads()[0];
          state.selectedThreadId = first ? String(first.id) : null;
        }
      } else {
        const visibleIds = new Set(visibleFacts().map((fact) => String(fact.id)));
        if (state.selectedFactId && !visibleIds.has(String(state.selectedFactId))) {
          state.selectedFactId = null;
        }
        if (!state.selectedPersonId && !state.selectedFactId) {
          const first = visibleFacts()[0];
          state.selectedFactId = first ? String(first.id) : null;
        }
      }
    }
    function renderList() {
      if (state.view === "events") {
        const shown = visibleThreads();
        listEl.innerHTML = shown.length ? shown.map((thread) => {
          const active = String(thread.id) === String(state.selectedThreadId) ? " active" : "";
          const peopleBadge = thread.people_label ? `<span class="badge">${escapeHtml(thread.people_label)}</span>` : "";
          return `<div class="item${active}" data-thread="${escapeHtml(thread.id)}">`
            + `<div class="item-title">${escapeHtml(thread.title || "未命名事件线")}</div>`
            + `<div class="item-summary">${escapeHtml(shortText(thread.current_summary || thread.details || "", 88))}</div>`
            + `<div class="badges">`
            + `<span class="badge">${escapeHtml(thread.status || "active")}</span>`
            + peopleBadge
            + `<span class="badge">key=${escapeHtml(thread.thread_key || thread.key || "")}</span>`
            + `<span class="badge">steps=${(stepsByThread.get(String(thread.id)) || []).length}</span>`
            + `</div></div>`;
        }).join("") : `<p class="small">没有匹配的事件线。</p>`;
        for (const item of listEl.querySelectorAll(".item")) {
          item.addEventListener("click", () => selectThread(item.dataset.thread));
        }
        return;
      }

      const shown = visibleFacts();
      listEl.innerHTML = shown.length ? shown.map((fact) => {
        const active = String(fact.id) === String(state.selectedFactId) && !state.selectedPersonId ? " active" : "";
        const subject = fact.subject_display_name || personLabel(fact.subject_person_key) || fact.subject_person_key || "";
        const object = fact.object_display_name || personLabel(fact.object_person_key) || fact.object_person_key || "";
        const scope = fact.scope === "relationship" ? "关系 fact" : "人物 fact";
        const title = fact.scope === "relationship" && object
          ? `${subject} → ${object}`
          : subject;
        return `<div class="item${active}" data-fact="${escapeHtml(fact.id)}">`
          + `<div class="item-title">${escapeHtml(title || "未命名 fact")}</div>`
          + `<div class="item-summary">${escapeHtml(shortText(`${fact.fact_key}: ${fact.fact_value}`, 96))}</div>`
          + `<div class="badges">`
          + `<span class="badge">${escapeHtml(scope)}</span>`
          + `<span class="badge">confidence=${Number(fact.confidence || 0).toFixed(2)}</span>`
          + `</div></div>`;
      }).join("") : `<p class="small">没有匹配的 facts。</p>`;
      for (const item of listEl.querySelectorAll(".item")) {
        item.addEventListener("click", () => selectFact(item.dataset.fact));
      }
    }
    function selectThread(threadId) {
      state.view = "events";
      state.selectedThreadId = String(threadId || "");
      state.selectedPersonId = null;
      updateAndDraw();
    }
    function selectFact(factId) {
      state.view = "facts";
      state.selectedFactId = String(factId || "");
      state.selectedPersonId = null;
      updateAndDraw();
    }
    function selectPerson(node) {
      state.view = state.view || "facts";
      state.selectedPersonId = node.id;
      state.selectedFactId = null;
      updateAndDraw();
    }
    function setView(view) {
      state.view = view === "facts" ? "facts" : "events";
      state.selectedPersonId = null;
      state.scale = 1;
      state.tx = 0;
      state.ty = 0;
      if (state.view === "facts") {
        state.factsLayoutKey = "";
      } else {
        stopFactsPhysics();
      }
      updateAndDraw();
      setTimeout(fitView, 20);
    }
    function filteredGraph() {
      if (state.view === "facts") {
        const visibleFactIds = visibleFactNodeIdSet();
        const linkedPeople = new Set();
        for (const fact of visibleFacts()) {
          if (fact.subject_person_key) linkedPeople.add(`person-${fact.subject_person_key}`);
          if (fact.object_person_key) linkedPeople.add(`person-${fact.object_person_key}`);
        }
        const nodes = allNodes.filter((node) => {
          if (node.type === "person") return linkedPeople.has(node.id);
          if (node.type === "fact") return visibleFactIds.has(node.id);
          return false;
        });
        const nodeIds = new Set(nodes.map((node) => node.id));
        const edges = allEdges.filter((edge) => {
          if (!["person_fact", "fact_subject", "fact_object", "relationship_fact"].includes(edge.type || "")) return false;
          return nodeIds.has(edge.source) && nodeIds.has(edge.target);
        });
        return { nodes, edges };
      }

      const visibleIds = visibleThreadIdSet();
      const linkedPeople = new Set();
      for (const edge of allEdges) {
        if (edge.type === "person_thread") {
          const target = byNodeId.get(edge.target);
          if (target && visibleIds.has(String(target.thread_id))) linkedPeople.add(edge.source);
        }
      }
      const nodes = allNodes.filter((node) => {
        if (node.type === "person") return linkedPeople.has(node.id);
        if (node.type === "thread") return visibleIds.has(String(node.thread_id));
        if (node.type === "step") return visibleIds.has(String(node.thread_id));
        return false;
      });
      const nodeIds = new Set(nodes.map((node) => node.id));
      const edges = allEdges.filter((edge) => {
        const isEventEdge = edge.type === "person_thread" || !!edge.step_type || !edge.type;
        return isEventEdge && nodeIds.has(edge.source) && nodeIds.has(edge.target);
      });
      return { nodes, edges };
    }
    function nodeRadius(node) {
      if (node.type === "thread") return 24;
      if (node.type === "person") return 20;
      if (node.type === "fact") return node.scope === "relationship" ? 18 : 16;
      return node.step_type === "time" ? 14 : 16;
    }
    function nodeColor(node) {
      if (node.type === "thread") return colors.thread;
      if (node.type === "person") return colors.person;
      if (node.type === "fact") return node.scope === "relationship" ? colors.relationship : colors.fact;
      return colors[node.step_type] || colors.system;
    }
    function placeNode(nodeId, x, y) {
      const node = byNodeId.get(nodeId);
      if (!node) return;
      node.x = x;
      node.y = y;
    }
    function layoutEvents(nodes) {
      const shown = visibleThreads();
      const people = nodes.filter((node) => node.type === "person");
      const centerX = 640;
      const centerY = 420;
      people.forEach((node, index) => {
        const angle = people.length <= 1 ? Math.PI : -Math.PI / 2 + (Math.PI * 2 * index) / people.length;
        const radius = people.length <= 1 ? 0 : 88;
        node.x = centerX + Math.cos(angle) * radius;
        node.y = centerY + Math.sin(angle) * radius;
      });

      const maxSteps = Math.max(1, ...shown.map((thread) => threadSteps(thread.id).length));
      const innerRadius = shown.length <= 1 ? 190 : Math.max(190, Math.min(430, shown.length * 24));
      const stepStart = shown.length <= 1 ? 165 : 155;
      const stepGap = Math.max(118, Math.min(148, 650 / Math.max(1, maxSteps)));
      shown.forEach((thread, index) => {
        const angle = shown.length <= 1 ? 0 : -Math.PI / 2 + (Math.PI * 2 * index) / shown.length;
        const ux = Math.cos(angle);
        const uy = Math.sin(angle);
        const threadX = centerX + ux * innerRadius;
        const threadY = centerY + uy * innerRadius;
        placeNode(`thread-${thread.id}`, threadX, threadY);
        threadSteps(thread.id).forEach((step, idx) => {
          placeNode(`step-${step.id}`, threadX + ux * (stepStart + idx * stepGap), threadY + uy * (stepStart + idx * stepGap));
        });
      });
    }
    function layoutFacts(nodes) {
      const people = nodes.filter((node) => node.type === "person");
      const factNodes = nodes.filter((node) => node.type === "fact");
      const centerX = 640;
      const centerY = 420;
      const layoutKey = nodes.map((node) => node.id).sort().join("|");
      const shouldReset = state.factsLayoutKey !== layoutKey;
      if (!shouldReset && nodes.every((node) => node._factsPlaced && Number.isFinite(node.x) && Number.isFinite(node.y))) {
        return;
      }
      state.factsLayoutKey = layoutKey;
      for (const node of nodes) {
        node.vx = 0;
        node.vy = 0;
      }
      const personRadius = people.length <= 1 ? 0 : Math.max(130, Math.min(340, people.length * 54));
      people.forEach((node, index) => {
        const angle = people.length <= 1 ? -Math.PI / 2 : -Math.PI / 2 + (Math.PI * 2 * index) / people.length;
        node.x = centerX + Math.cos(angle) * personRadius;
        node.y = centerY + Math.sin(angle) * personRadius;
        node._factsPlaced = true;
      });
      const perSubjectCount = new Map();
      factNodes.forEach((node, index) => {
        const subject = byNodeId.get(`person-${node.subject_person_key || ""}`);
        const object = node.object_person_key ? byNodeId.get(`person-${node.object_person_key}`) : null;
        if (node.scope === "relationship" && subject && object) {
          const dx = object.x - subject.x;
          const dy = object.y - subject.y;
          const len = Math.hypot(dx, dy) || 1;
          const offset = ((index % 5) - 2) * 18;
          node.x = (subject.x + object.x) / 2 + (-dy / len) * offset;
          node.y = (subject.y + object.y) / 2 + (dx / len) * offset;
          node._factsPlaced = true;
          return;
        }
        if (subject) {
          const key = node.subject_person_key || "_";
          const count = perSubjectCount.get(key) || 0;
          perSubjectCount.set(key, count + 1);
          const angle = -Math.PI / 2 + count * 0.72;
          const radius = 132 + Math.floor(count / 8) * 54;
          node.x = subject.x + Math.cos(angle) * radius;
          node.y = subject.y + Math.sin(angle) * radius;
          node._factsPlaced = true;
          return;
        }
        const angle = -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(1, factNodes.length);
        node.x = centerX + Math.cos(angle) * 220;
        node.y = centerY + Math.sin(angle) * 220;
        node._factsPlaced = true;
      });
    }
    function applyLayout(nodes) {
      if (state.view === "facts") layoutFacts(nodes);
      else layoutEvents(nodes);
    }
    function graphTransform() {
      return `translate(${state.tx},${state.ty}) scale(${state.scale})`;
    }
    function applyTransform() {
      if (state.viewport) state.viewport.setAttribute("transform", graphTransform());
    }
    function updateEdgePositions() {
      for (const edge of state.visibleEdges) {
        const edgeId = edge.id || `${edge.source}->${edge.target}`;
        const lines = state.edgeEls.get(edgeId) || [];
        const source = byNodeId.get(edge.source);
        const target = byNodeId.get(edge.target);
        if (!lines.length || !source || !target) continue;
        for (const line of lines) {
          line.setAttribute("x1", source.x);
          line.setAttribute("y1", source.y);
          line.setAttribute("x2", target.x);
          line.setAttribute("y2", target.y);
        }
      }
    }
    function updateNodePosition(node) {
      const group = state.nodeEls.get(node.id);
      if (group) group.setAttribute("transform", `translate(${node.x},${node.y})`);
    }
    function updateGraphPositions() {
      for (const node of state.visibleNodes) updateNodePosition(node);
      updateEdgePositions();
    }
    function stopFactsPhysics() {
      if (state.physicsFrame) cancelAnimationFrame(state.physicsFrame);
      state.physicsFrame = 0;
      state.physicsRunning = false;
    }
    function factsSpringLength(edge) {
      if (edge.type === "relationship_fact") return 230;
      if (edge.type === "fact_subject" || edge.type === "fact_object") return 125;
      if (edge.type === "person_fact") return 145;
      return 150;
    }
    function factsPhysicsStep() {
      if (state.view !== "facts" || !state.visibleNodes.length) {
        stopFactsPhysics();
        return;
      }
      const nodes = state.visibleNodes;
      const visibleIds = new Set(nodes.map((node) => node.id));
      const forces = new Map(nodes.map((node) => [node.id, { x: 0, y: 0 }]));
      const centerX = nodes.reduce((sum, node) => sum + node.x, 0) / Math.max(1, nodes.length);
      const centerY = nodes.reduce((sum, node) => sum + node.y, 0) / Math.max(1, nodes.length);

      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = nodes[i], b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let distSq = dx * dx + dy * dy;
          if (distSq < 0.01) {
            dx = 0.1 + i * 0.01;
            dy = 0.1 + j * 0.01;
            distSq = dx * dx + dy * dy;
          }
          const dist = Math.sqrt(distSq);
          const strength = (a.type === "person" && b.type === "person") ? 2800 : 1900;
          const force = Math.min(2.8, strength / distSq);
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          forces.get(a.id).x -= fx;
          forces.get(a.id).y -= fy;
          forces.get(b.id).x += fx;
          forces.get(b.id).y += fy;
        }
      }

      for (const edge of state.visibleEdges) {
        if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
        const source = byNodeId.get(edge.source);
        const target = byNodeId.get(edge.target);
        if (!source || !target) continue;
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const dist = Math.hypot(dx, dy) || 1;
        const targetLen = factsSpringLength(edge);
        const spring = edge.type === "relationship_fact" ? 0.015 : 0.035;
        const force = (dist - targetLen) * spring;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        forces.get(source.id).x += fx;
        forces.get(source.id).y += fy;
        forces.get(target.id).x -= fx;
        forces.get(target.id).y -= fy;
      }

      let maxVelocity = 0;
      for (const node of nodes) {
        if (node === state.draggingNode || node.fx !== null || node.fy !== null) {
          node.x = Number.isFinite(node.fx) ? node.fx : node.x;
          node.y = Number.isFinite(node.fy) ? node.fy : node.y;
          node.vx = 0;
          node.vy = 0;
          continue;
        }
        const force = forces.get(node.id) || { x: 0, y: 0 };
        force.x += (centerX - node.x) * 0.004;
        force.y += (centerY - node.y) * 0.004;
        node.vx = (node.vx + force.x) * 0.84;
        node.vy = (node.vy + force.y) * 0.84;
        node.x += node.vx;
        node.y += node.vy;
        maxVelocity = Math.max(maxVelocity, Math.hypot(node.vx, node.vy));
      }

      updateGraphPositions();
      if (maxVelocity > 0.04 || state.draggingNode) {
        state.physicsFrame = requestAnimationFrame(factsPhysicsStep);
      } else {
        state.physicsFrame = 0;
        state.physicsRunning = false;
      }
    }
    function startFactsPhysics() {
      if (state.view !== "facts") return;
      if (state.physicsRunning) return;
      state.physicsRunning = true;
      state.physicsFrame = requestAnimationFrame(factsPhysicsStep);
    }
    function setPanning(active) {
      state.panning = active;
      svg.classList.toggle("is-panning", active);
    }
    function moveDraggedNode(ev) {
      const node = state.draggingNode;
      if (!node || state.activeNodePointerId !== ev.pointerId) return false;
      ev.preventDefault();
      const pt = toGraphPoint(ev);
      if (Math.abs(node.x - pt.x) > 1 || Math.abs(node.y - pt.y) > 1) state.suppressClick = true;
      node.x = pt.x;
      node.y = pt.y;
      if (state.view === "facts") {
        node.fx = pt.x;
        node.fy = pt.y;
        updateGraphPositions();
        startFactsPhysics();
      } else {
        updateNodePosition(node);
        updateEdgePositions();
      }
      return true;
    }
    function endDraggedNode(ev) {
      const node = state.draggingNode;
      if (!node || state.activeNodePointerId !== ev.pointerId) return false;
      state.draggingNode = null;
      state.activeNodePointerId = null;
      node.fx = null;
      node.fy = null;
      const group = state.nodeEls.get(node.id);
      if (group) {
        try { group.releasePointerCapture(ev.pointerId); } catch (_e) {}
        group.style.cursor = "grab";
      }
      if (state.view === "facts") startFactsPhysics();
      return true;
    }
    function selectedNodeIdSet() {
      const ids = new Set();
      if (state.view === "events" && state.selectedThreadId) {
        ids.add(`thread-${state.selectedThreadId}`);
        for (const step of threadSteps(state.selectedThreadId)) ids.add(`step-${step.id}`);
      }
      if (state.view === "facts") {
        if (state.selectedFactId) ids.add(`fact-${state.selectedFactId}`);
        if (state.selectedPersonId) ids.add(state.selectedPersonId);
      }
      return ids;
    }
    function draw() {
      const { nodes, edges } = filteredGraph();
      state.visibleNodes = nodes;
      state.visibleEdges = edges;
      state.edgeEls = new Map();
      state.nodeEls = new Map();
      empty.textContent = state.view === "events"
        ? "没有匹配的事件线。"
        : "没有匹配的人物 facts。";
      empty.hidden = nodes.length > 0;
      applyLayout(nodes);
      svg.innerHTML = "";
      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      state.viewport = g;
      applyTransform();
      svg.appendChild(g);
      const selectedIds = selectedNodeIdSet();
      for (const edge of edges) {
        const source = byNodeId.get(edge.source);
        const target = byNodeId.get(edge.target);
        if (!source || !target) continue;
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        const edgeId = edge.id || `${edge.source}->${edge.target}`;
        line.setAttribute("x1", source.x);
        line.setAttribute("y1", source.y);
        line.setAttribute("x2", target.x);
        line.setAttribute("y2", target.y);
        const selectedEdge = selectedIds.has(edge.source)
          || selectedIds.has(edge.target)
          || (edge.fact_id && String(edge.fact_id) === String(state.selectedFactId));
        const edgeKind = edge.type === "relationship_fact"
          ? " relationship"
          : (edge.type || "").includes("fact")
            ? " fact"
            : "";
        line.setAttribute("class", "link" + edgeKind + (selectedEdge ? " selected" : ""));
        const edgeLines = state.edgeEls.get(edgeId) || [];
        edgeLines.push(line);
        state.edgeEls.set(edgeId, edgeLines);
        g.appendChild(line);
      }
      for (const node of nodes) {
        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
        group.setAttribute("class", "node" + (selectedIds.has(node.id) ? " selected" : ""));
        group.setAttribute("transform", `translate(${node.x},${node.y})`);
        group.style.cursor = "grab";
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("r", nodeRadius(node));
        circle.setAttribute("fill", nodeColor(node));
        group.appendChild(circle);
        const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
        text.setAttribute("x", nodeRadius(node) + 7);
        text.setAttribute("y", 4);
        text.textContent = shortText(node.label || node.summary || node.key || "", node.type === "thread" ? 26 : node.type === "fact" ? 34 : 30);
        group.appendChild(text);
        group.addEventListener("pointerdown", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          state.draggingNode = node;
          state.activeNodePointerId = ev.pointerId;
          state.suppressClick = false;
          if (state.view === "facts") {
            node.fx = node.x;
            node.fy = node.y;
            startFactsPhysics();
          }
          try { group.setPointerCapture(ev.pointerId); } catch (_e) {}
          group.style.cursor = "grabbing";
        });
        group.addEventListener("pointermove", (ev) => {
          moveDraggedNode(ev);
        });
        const endNodeDrag = (ev) => {
          endDraggedNode(ev);
        };
        group.addEventListener("pointerup", endNodeDrag);
        group.addEventListener("pointercancel", endNodeDrag);
        group.addEventListener("lostpointercapture", () => {
          if (state.draggingNode === node) {
            state.draggingNode = null;
            node.fx = null;
            node.fy = null;
            if (state.view === "facts") startFactsPhysics();
          }
          state.activeNodePointerId = null;
          group.style.cursor = "grab";
        });
        group.addEventListener("click", () => {
          if (state.suppressClick) {
            state.suppressClick = false;
            return;
          }
          if (node.type === "thread" || node.type === "step") selectThread(node.thread_id);
          else if (node.type === "fact") selectFact(node.fact_id);
          else if (node.type === "person") selectPerson(node);
        });
        state.nodeEls.set(node.id, group);
        g.appendChild(group);
      }
      if (state.view === "facts") startFactsPhysics();
    }
    function toGraphPoint(ev) {
      const rect = svg.getBoundingClientRect();
      return {
        x: (ev.clientX - rect.left - state.tx) / state.scale,
        y: (ev.clientY - rect.top - state.ty) / state.scale,
      };
    }
    function fitView() {
      const { nodes } = filteredGraph();
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
    }
    function renderEventDetail() {
      const thread = threads.find((item) => String(item.id) === String(state.selectedThreadId));
      if (!thread) {
        detailEl.innerHTML = `<h2>没有选中事件线</h2><p class="small">点击左侧事件线或图谱节点查看详情。</p>`;
        return;
      }
      const shownSteps = threadSteps(thread.id);
      detailEl.innerHTML = `
        <h2>${escapeHtml(thread.title || "未命名事件线")}</h2>
        <div class="small">key=${escapeHtml(thread.thread_key || thread.key || "")}</div>
        <div class="badges">
          <span class="badge">${escapeHtml(thread.status || "active")}</span>
          <span class="badge">${escapeHtml(thread.kind || "event")}</span>
          ${thread.people_label ? `<span class="badge">${escapeHtml(thread.people_label)}</span>` : ""}
          <span class="badge">confidence=${Number(thread.confidence || 0).toFixed(2)}</span>
        </div>
        <div class="detail-block">
          <div class="small">当前状态</div>
          <p>${escapeHtml(thread.current_summary || thread.details || "暂无当前状态")}</p>
          ${thread.followup_hint ? `<div class="small">跟进提示</div><p>${escapeHtml(thread.followup_hint)}</p>` : ""}
        </div>
        <div class="detail-block">
          <div class="small">进展时间线 · ${shownSteps.length} 条</div>
          <div class="timeline">
            ${shownSteps.map((step, idx) => `
              <div class="step ${escapeHtml(step.step_type || "user")}">
                <div class="step-head">
                  <strong>#${idx + 1} · ${escapeHtml(stepLabel(step.step_type))}</strong>
                  <span>${escapeHtml(step.occurred_at || step.created_at || "")}</span>
                </div>
                <div class="step-summary">${escapeHtml(step.summary || "")}</div>
                ${step.people_label ? `<div class="small">人物：${escapeHtml(step.people_label)}</div>` : ""}
                ${step.cause ? `<div class="small">触发：${escapeHtml(step.cause)}</div>` : ""}
                ${step.reflection ? `<div class="small">反思：${escapeHtml(step.reflection)}</div>` : ""}
              </div>
            `).join("")}
          </div>
        </div>`;
    }
    function renderFactDetail() {
      if (state.selectedPersonId) {
        const node = byNodeId.get(state.selectedPersonId);
        const personKey = node?.person_key || state.selectedPersonId.replace(/^person-/, "");
        const related = facts.filter((fact) => fact.subject_person_key === personKey || fact.object_person_key === personKey);
        detailEl.innerHTML = `
          <h2>${escapeHtml(node?.label || personKey || "人物")}</h2>
          <div class="small">${escapeHtml(node?.kind || "person")}</div>
          <div class="detail-block">
            <div class="small">相关 facts · ${related.length} 条</div>
            <div class="timeline">
              ${related.map((fact) => `
                <div class="step">
                  <div class="step-head">
                    <strong>${escapeHtml(fact.scope === "relationship" ? "关系 fact" : "人物 fact")}</strong>
                    <span>${escapeHtml(fact.updated_at || "")}</span>
                  </div>
                  <div class="step-summary">${escapeHtml(fact.fact_key || "")}: ${escapeHtml(fact.fact_value || "")}</div>
                  <div class="small">${escapeHtml(fact.subject_display_name || personLabel(fact.subject_person_key) || fact.subject_person_key || "")}${fact.object_person_key ? " → " + escapeHtml(fact.object_display_name || personLabel(fact.object_person_key) || fact.object_person_key || "") : ""}</div>
                </div>
              `).join("") || `<p class="small">这个人物还没有 facts。</p>`}
            </div>
          </div>`;
        return;
      }
      const fact = factsById.get(String(state.selectedFactId));
      if (!fact) {
        detailEl.innerHTML = `<h2>没有选中 fact</h2><p class="small">点击左侧 fact 或图谱节点查看详情。</p>`;
        return;
      }
      const subject = fact.subject_display_name || personLabel(fact.subject_person_key) || fact.subject_person_key || "";
      const object = fact.object_display_name || personLabel(fact.object_person_key) || fact.object_person_key || "";
      detailEl.innerHTML = `
        <h2>${escapeHtml(fact.fact_key || "未命名 fact")}</h2>
        <div class="small">${escapeHtml(fact.scope === "relationship" ? "关系 fact" : "人物 fact")}</div>
        <div class="badges">
          <span class="badge">confidence=${Number(fact.confidence || 0).toFixed(2)}</span>
          ${fact.updated_at ? `<span class="badge">${escapeHtml(fact.updated_at)}</span>` : ""}
        </div>
        <div class="detail-block">
          <div class="small">人物</div>
          <p>${escapeHtml(subject || "未知人物")}${object ? " → " + escapeHtml(object) : ""}</p>
        </div>
        <div class="detail-block">
          <div class="small">内容</div>
          <p>${escapeHtml(fact.fact_value || "")}</p>
        </div>`;
    }
    function renderDetail() {
      if (state.view === "facts") renderFactDetail();
      else renderEventDetail();
    }
    function updateAndDraw() {
      stopFactsPhysics();
      ensureSelection();
      updateControls();
      renderList();
      renderDetail();
      draw();
    }
    searchEl.addEventListener("input", (ev) => {
      state.query = ev.target.value || "";
      updateAndDraw();
      setTimeout(fitView, 20);
    });
    statusEl.addEventListener("change", (ev) => {
      state.status = ev.target.value || "";
      updateAndDraw();
      setTimeout(fitView, 20);
    });
    document.getElementById("view-events").addEventListener("click", () => setView("events"));
    document.getElementById("view-facts").addEventListener("click", () => setView("facts"));
    document.getElementById("fit").addEventListener("click", fitView);
    svg.addEventListener("wheel", (ev) => {
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
    }, { passive: false });
    function updatePinch() {
      const points = Array.from(state.activePointers.values());
      if (points.length < 2) {
        state.pinch = null;
        return;
      }
      const a = points[0], b = points[1];
      const cx = (a.x + b.x) / 2;
      const cy = (a.y + b.y) / 2;
      const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      if (!state.pinch) {
        state.pinch = {
          cx,
          cy,
          distance,
          scale: state.scale,
          tx: state.tx,
          ty: state.ty,
        };
        return;
      }
      const nextScale = Math.min(3.2, Math.max(0.25, state.pinch.scale * (distance / state.pinch.distance)));
      const graphX = (state.pinch.cx - state.pinch.tx) / state.pinch.scale;
      const graphY = (state.pinch.cy - state.pinch.ty) / state.pinch.scale;
      state.scale = nextScale;
      state.tx = cx - graphX * nextScale;
      state.ty = cy - graphY * nextScale;
      applyTransform();
    }
    svg.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      state.activePointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (state.activePointers.size >= 2) {
        setPanning(false);
        state.lastPointer = null;
        updatePinch();
      } else {
        setPanning(true);
        state.lastPointer = { x: ev.clientX, y: ev.clientY };
      }
      try { svg.setPointerCapture(ev.pointerId); } catch (_e) {}
    });
    svg.addEventListener("pointermove", (ev) => {
      if (moveDraggedNode(ev)) return;
      if (!state.activePointers.has(ev.pointerId)) return;
      ev.preventDefault();
      state.activePointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (state.activePointers.size >= 2) {
        updatePinch();
        return;
      }
      if (!state.panning || !state.lastPointer || state.draggingNode) return;
      state.tx += ev.clientX - state.lastPointer.x;
      state.ty += ev.clientY - state.lastPointer.y;
      state.lastPointer = { x: ev.clientX, y: ev.clientY };
      applyTransform();
    });
    function endCanvasPointer(ev) {
      if (endDraggedNode(ev)) return;
      state.activePointers.delete(ev.pointerId);
      if (state.activePointers.size >= 2) {
        updatePinch();
      } else if (state.activePointers.size === 1) {
        const only = Array.from(state.activePointers.values())[0];
        state.pinch = null;
        setPanning(true);
        state.lastPointer = { x: only.x, y: only.y };
      } else {
        state.pinch = null;
        setPanning(false);
        state.lastPointer = null;
      }
      try { svg.releasePointerCapture(ev.pointerId); } catch (_e) {}
    }
    svg.addEventListener("pointerup", endCanvasPointer);
    svg.addEventListener("pointercancel", endCanvasPointer);
    svg.addEventListener("lostpointercapture", (ev) => {
      state.activePointers.delete(ev.pointerId);
      if (!state.activePointers.size) {
        state.pinch = null;
        setPanning(false);
        state.lastPointer = null;
      }
    });
    svg.addEventListener("dblclick", (ev) => {
      ev.preventDefault();
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const oldScale = state.scale;
      state.scale = Math.min(3.2, state.scale * 1.35);
      state.tx = mx - ((mx - state.tx) / oldScale) * state.scale;
      state.ty = my - ((my - state.ty) / oldScale) * state.scale;
      applyTransform();
    });
    window.addEventListener("resize", () => {
      updateGraphPositions();
      applyTransform();
    });
    updateAndDraw();
    setTimeout(fitView, 80);
  </script>
</body>
</html>
"""
    return (
        template.replace("__TITLE__", html.escape(title))
        .replace("__DATA_JSON__", _json_script_payload(payload))
    )
