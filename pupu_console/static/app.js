/* global fetch, WebSocket, document, window */

const $ = (sel) => document.querySelector(sel);

let selectedId = null;
let logTimer = null;
let arbiterTimer = null;
let ws = null;
let currentEventGraph = null;
let selectedEventThreadId = null;
let eventGraphViewState = { scale: 1, tx: 0, ty: 0 };
let eventGraphLayoutMode = "horizontal";

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("application/json")) return r.json();
  return r.text();
}

async function loadSoulsIntoSelect() {
  const souls = await api("/api/souls");
  const sel = $("#new-soul-select");
  if (!sel) return;
  sel.innerHTML = '<option value="">（空白）</option>';
  for (const s of souls) {
    const o = document.createElement("option");
    o.value = s.slug;
    o.textContent = `${s.display_name} (${s.slug})`;
    sel.appendChild(o);
  }
}

async function refreshArbiter() {
  const dot = $("#arbiter-dot");
  const detail = $("#arbiter-detail");
  const btnS = $("#btn-arbiter-start");
  const btnP = $("#btn-arbiter-stop");
  const openL = $("#arbiter-open");
  if (!dot || !detail || !btnS || !btnP) return;
  try {
    const a = await api("/api/arbiter");
    const bind = String(a.bind || "").trim();
    dot.className = "dot " + (a.running ? "on" : "off");
    if (a.running) {
      const pidPart = a.pid != null ? `pid=${a.pid} · ` : "";
      detail.textContent = `运行中 · ${pidPart}${bind || "—"}`;
      if (openL && bind) {
        openL.hidden = false;
        const base = bind.replace(/\/$/, "");
        openL.href = `${base}/health`;
      } else if (openL) {
        openL.hidden = true;
      }
    } else {
      const ex = a.exit_code != null && a.exit_code !== undefined ? ` · 退出码 ${a.exit_code}` : "";
      detail.textContent = bind ? `未运行${ex} · ${bind}` : `未运行${ex}`;
      if (openL) openL.hidden = true;
    }
    btnS.disabled = !!a.running;
    btnP.disabled = !a.running;
  } catch (e) {
    detail.textContent = "状态获取失败";
    console.warn(e);
  }
}

async function loadInstances() {
  const list = await api("/api/instances");
  const el = $("#instance-list");
  el.innerHTML = "";
  for (const it of list) {
    const div = document.createElement("div");
    div.className = "inst-item" + (it.id === selectedId ? " active" : "");
    div.dataset.id = it.id;
    div.innerHTML =
      `<span class="dot ${it.running ? "on" : "off"}"></span>` +
      `<strong>${escapeHtml(it.display_name)}</strong>` +
      `<div class="sub">:${it.port} · ${escapeHtml(it.qq_mode)}</div>`;
    div.addEventListener("click", () => selectInstance(it.id));
    el.appendChild(div);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shortText(s, max = 34) {
  const text = String(s || "").replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return text.slice(0, Math.max(1, max - 1)) + "…";
}

function stepTypeLabel(type) {
  const map = {
    user: "用户",
    instance: "实例",
    time: "时间",
    system: "系统",
  };
  return map[type] || type || "进展";
}

function stopLogPoll() {
  if (logTimer) {
    clearInterval(logTimer);
    logTimer = null;
  }
  if (ws) {
    try {
      ws.close();
    } catch (e) {
      /* ignore */
    }
    ws = null;
  }
}

async function pollLogs(id) {
  try {
    const data = await api(`/api/instances/${id}/logs?tail=300`);
    const box = $("#logbox");
    if (box) box.textContent = data.text || "";
  } catch (e) {
    console.warn(e);
  }
}

function startLogStream(id) {
  stopLogPoll();
  pollLogs(id);
  logTimer = setInterval(() => pollLogs(id), 2500);
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${window.location.host}/ws/instances/${id}/console`;
  try {
    ws = new WebSocket(url);
    ws.onmessage = (ev) => {
      const box = $("#logbox");
      if (!box) return;
      box.textContent += ev.data;
      box.scrollTop = box.scrollHeight;
    };
  } catch (e) {
    console.warn("ws", e);
  }
}

async function selectInstance(id) {
  selectedId = id;
  await loadInstances();
  const data = await api(`/api/instances/${id}`);
  const p = data.persona || {};
  const main = $("#main-panel");
  main.innerHTML = `
    <h2>${escapeHtml(data.display_name || id)}</h2>
    <div class="tabs">
      <button type="button" class="tab active" data-tab="run">运行</button>
      <button type="button" class="tab" data-tab="soul">灵魂</button>
      <button type="button" class="tab" data-tab="events">事件图谱</button>
    </div>
    <div id="tab-run" class="panel">
      <p>状态: <span id="run-status"></span></p>
      <div class="row">
        <button type="button" class="primary" id="btn-start">启动</button>
        <button type="button" id="btn-stop">停止</button>
        <button type="button" class="danger" id="btn-del">删除实例</button>
      </div>
      <h3>记忆 (SQLite)</h3>
      <p class="hint">记忆数据库路径（<code>pupu.db</code>）：</p>
      <pre class="memory-path" id="memory-path">${escapeHtml(data.memory_path || "")}</pre>
      <p id="memory-exists" class="hint"></p>
      <label>
        导入记忆文件（完全覆盖当前数据库）
        <input type="file" id="memory-file" accept=".db,.sqlite,application/octet-stream" />
      </label>
      <div class="row">
        <button type="button" class="primary" id="btn-import-memory" ${data.running ? "disabled" : ""}>导入并覆盖</button>
      </div>
      <p id="memory-msg" class="hint"></p>
      <h3>控制台输出</h3>
      <div class="logbox" id="logbox"></div>
    </div>
    <div id="tab-soul" class="panel" style="display:none">
      <label>显示名称 <input id="f-display" value="${escapeHtml(data.display_name || "")}" /></label>
      <label>端口 <input id="f-port" type="number" value="${Number(data.port) || 8081}" /></label>
      <label>QQ 模式
        <select id="f-qqmode">
          <option value="napcat">napcat</option>
          <option value="official">official</option>
          <option value="cli">cli</option>
        </select>
      </label>
      <label>qq_app_id <input id="f-appid" value="${escapeHtml(data.qq_app_id || "")}" /></label>
      <label>qq_app_secret <input id="f-secret" type="password" value="${escapeHtml(data.qq_app_secret || "")}" /></label>
      <label>owner_ids（逗号分隔）<input id="f-owners" value="${escapeHtml((data.owner_ids || []).join(","))}" /></label>
      <section class="settings-block">
        <h3>私聊白名单</h3>
        <label>私聊回复策略
          <select id="f-private-mode">
            <option value="owner_only">只回复 owner_ids</option>
            <option value="allowlist">回复 owner_ids 和白名单</option>
            <option value="all">回复所有私聊</option>
          </select>
        </label>
        <label>白名单 QQ（逗号、空格或换行分隔）
          <textarea id="f-private-allowed" class="small-textarea" placeholder="123456&#10;987654">${escapeHtml((data.private_allowed_ids || []).join("\n"))}</textarea>
        </label>
        <p class="hint">命令权限仍只看 owner_ids；这里仅控制普通私聊是否进入聊天回复。</p>
      </section>
      <label>名字 <input id="f-name" value="${escapeHtml(p.name || "")}" /></label>
      <label>core_persona<textarea id="f-core">${escapeHtml(p.core_persona || "")}</textarea></label>
      <label>seed_self_facts（每行 key=value）<textarea id="f-facts">${escapeHtml(formatFacts(p.seed_self_facts))}</textarea></label>
      <div class="row">
        <button type="button" class="primary" id="btn-save-soul">保存</button>
      </div>
      <hr />
      <h3>预设</h3>
      <label>从预设载入
        <select id="apply-soul-slug"></select>
        <button type="button" id="btn-apply-soul">载入到本实例</button>
      </label>
      <label>另存为预设 slug <input id="cap-slug" placeholder="my_soul" /></label>
      <label>预设显示名 <input id="cap-dname" placeholder="我的预设" /></label>
      <button type="button" id="btn-capture">另存为预设</button>
    </div>
    <div id="tab-events" class="panel" style="display:none">
      <div class="event-toolbar">
        <div>
          <h3>事件图谱</h3>
          <p class="hint" id="event-graph-status">尚未加载。</p>
        </div>
        <button type="button" id="btn-refresh-events">刷新</button>
      </div>
      <div id="event-graph-panel" class="event-graph-empty">切换到此页后会加载事件线。</div>
    </div>
  `;
  $("#f-qqmode").value = data.qq_mode || "napcat";
  $("#f-private-mode").value = data.private_reply_mode || "owner_only";

  const mex = $("#memory-exists");
  if (mex) {
    mex.textContent = data.memory_db_exists
      ? "当前已有记忆数据库文件。"
      : "尚未生成记忆文件（实例首次运行聊天后会创建 pupu.db）。仍可导入外部备份。";
  }

  main.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      main.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const t = btn.dataset.tab;
      $("#tab-run").style.display = t === "run" ? "block" : "none";
      $("#tab-soul").style.display = t === "soul" ? "block" : "none";
      $("#tab-events").style.display = t === "events" ? "block" : "none";
      if (t === "events") {
        loadEventGraph(id);
      }
    });
  });

  $("#btn-start").addEventListener("click", async () => {
    await api(`/api/instances/${id}/start`, { method: "POST" });
    await refreshRunStatus();
    await loadInstances();
  });
  $("#btn-stop").addEventListener("click", async () => {
    await api(`/api/instances/${id}/stop`, { method: "POST" });
    await refreshRunStatus();
    await loadInstances();
  });
  $("#btn-import-memory").addEventListener("click", async () => {
    const fin = $("#memory-file");
    const msg = $("#memory-msg");
    if (!fin.files || !fin.files[0]) {
      alert("请先选择 .db 或 .sqlite 文件");
      return;
    }
    if (
      !window.confirm(
        "将用所选文件完全覆盖当前实例的 pupu.db（原文件会备份为 pupu.db.bak.*）。确定继续？"
      )
    ) {
      return;
    }
    msg.textContent = "导入中…";
    try {
      const res = await apiImportMemory(id, fin.files[0]);
      msg.textContent = res.message || "导入完成";
      fin.value = "";
      await refreshRunStatus();
    } catch (e) {
      msg.textContent = "失败: " + e.message;
    }
  });
  $("#btn-del").addEventListener("click", async () => {
    if (!window.confirm("确定删除该实例目录？（会移到 _trash）")) return;
    stopLogPoll();
    await api(`/api/instances/${id}`, { method: "DELETE" });
    selectedId = null;
    $("#main-panel").innerHTML = '<p class="hint">左侧选择一个仆仆实例。</p>';
    await loadInstances();
  });

  $("#btn-save-soul").addEventListener("click", () => saveSoulForm(id));
  $("#btn-refresh-events").addEventListener("click", () => loadEventGraph(id, { force: true }));

  const soulSel = $("#apply-soul-slug");
  const souls = await api("/api/souls");
  soulSel.innerHTML = '<option value="">选择…</option>';
  for (const s of souls) {
    const o = document.createElement("option");
    o.value = s.slug;
    o.textContent = `${s.display_name} (${s.slug})`;
    soulSel.appendChild(o);
  }
  $("#btn-apply-soul").addEventListener("click", async () => {
    const slug = soulSel.value;
    if (!slug) return;
    await api(`/api/instances/${id}/apply_soul`, {
      method: "POST",
      body: JSON.stringify({ slug }),
    });
    await selectInstance(id);
  });
  $("#btn-capture").addEventListener("click", async () => {
    const slug = $("#cap-slug").value.trim();
    const dname = $("#cap-dname").value.trim() || slug;
    if (!slug) return alert("填写 slug");
    await api("/api/souls", {
      method: "POST",
      body: JSON.stringify({ slug, display_name: dname, from_instance_id: id }),
    });
    alert("已保存预设 " + slug);
    await loadSoulsIntoSelect();
  });

  await refreshRunStatus();
  startLogStream(id);
}

async function loadEventGraph(id, opts = {}) {
  const status = $("#event-graph-status");
  const panel = $("#event-graph-panel");
  if (!panel) return;
  if (status) status.textContent = "加载中…";
  try {
    const data = await api(`/api/instances/${id}/event_graph`);
    currentEventGraph = data;
    const threads = data.threads || [];
    if (!threads.some((t) => String(t.id) === String(selectedEventThreadId))) {
      selectedEventThreadId = threads[0] ? threads[0].id : null;
    }
    renderEventGraph(data);
    if (status) {
      status.textContent = data.exists
        ? `事件线 ${threads.length} 条，进展 ${(data.steps || []).length} 条。`
        : "当前实例还没有记忆数据库。";
    }
  } catch (e) {
    currentEventGraph = null;
    panel.innerHTML = `<p class="hint">事件图谱加载失败：${escapeHtml(e.message || e)}</p>`;
    if (status) status.textContent = "加载失败。";
    if (!opts.force) console.warn(e);
  }
}

function renderEventGraph(data) {
  const panel = $("#event-graph-panel");
  if (!panel) return;
  const threads = data.threads || [];
  const steps = data.steps || [];
  if (!data.exists) {
    panel.innerHTML = "<p class='hint'>当前实例还没有生成 pupu.db。</p>";
    return;
  }
  if (!threads.length) {
    panel.innerHTML = "<p class='hint'>还没有事件线。新的 batch review 会写入 event_threads / event_steps。</p>";
    return;
  }

  const stepsByThread = new Map();
  for (const step of steps) {
    const key = String(step.thread_id);
    if (!stepsByThread.has(key)) stepsByThread.set(key, []);
    stepsByThread.get(key).push(step);
  }
  const selected = threads.find((t) => String(t.id) === String(selectedEventThreadId)) || threads[0];
  selectedEventThreadId = selected ? selected.id : null;
  const selectedSteps = selected ? stepsByThread.get(String(selected.id)) || [] : [];

  const listHtml = threads
    .map((thread) => {
      const active = String(thread.id) === String(selectedEventThreadId) ? " active" : "";
      const count = (stepsByThread.get(String(thread.id)) || []).length;
      const people = thread.people_label ? ` · ${escapeHtml(thread.people_label)}` : "";
      return `<button type="button" class="event-thread${active}" data-thread-id="${thread.id}">
        <strong>${escapeHtml(thread.title || "未命名事件")}</strong>
        <span>${escapeHtml(thread.status || "active")} · ${count} 步${people}</span>
        <code>${escapeHtml(thread.source_event_key || thread.key || "")}</code>
      </button>`;
    })
    .join("");

  panel.innerHTML = `
    <div class="event-layout">
      <aside class="event-thread-list">${listHtml}</aside>
      <section class="event-visual">
        <div class="event-graph-controls" role="group" aria-label="事件图谱布局">
          <button type="button" class="event-layout-mode${eventGraphLayoutMode === "horizontal" ? " active" : ""}" data-layout-mode="horizontal">横向事件链</button>
          <button type="button" class="event-layout-mode${eventGraphLayoutMode === "radial" ? " active" : ""}" data-layout-mode="radial">中心发散</button>
        </div>
        ${renderEventGraphSvg(threads, stepsByThread)}
        ${renderEventTimeline(selected, selectedSteps)}
      </section>
    </div>
  `;
  panel.querySelectorAll(".event-thread").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedEventThreadId = btn.dataset.threadId;
      renderEventGraph(currentEventGraph);
    });
  });
  panel.querySelectorAll(".event-layout-mode").forEach((btn) => {
    btn.addEventListener("click", () => {
      eventGraphLayoutMode = btn.dataset.layoutMode || "horizontal";
      eventGraphViewState = { scale: 1, tx: 0, ty: 0 };
      renderEventGraph(currentEventGraph);
    });
  });
  setupEventGraphInteractions(panel);
}

function renderEventGraphSvg(threads, stepsByThread) {
  if (eventGraphLayoutMode === "radial") {
    return renderEventGraphRadialSvg(threads, stepsByThread);
  }
  return renderEventGraphHorizontalSvg(threads, stepsByThread);
}

function renderEventGraphHorizontalSvg(threads, stepsByThread) {
  const maxSteps = Math.max(1, ...threads.map((t) => (stepsByThread.get(String(t.id)) || []).length));
  const width = Math.max(760, 260 + maxSteps * 170);
  const height = Math.max(240, 70 + threads.length * 112);
  const lines = [];
  const nodes = [];
  threads.forEach((thread, row) => {
    const y = 55 + row * 112;
    const threadX = 95;
    const steps = stepsByThread.get(String(thread.id)) || [];
    const selectedClass = String(thread.id) === String(selectedEventThreadId) ? " selected" : "";
    nodes.push(`
      <g class="graph-node thread-node${selectedClass}">
        <rect x="${threadX - 74}" y="${y - 24}" width="148" height="48" rx="6"></rect>
        <text x="${threadX}" y="${y - 4}" text-anchor="middle">${escapeHtml(shortText(thread.title || "未命名事件", 12))}</text>
        <text x="${threadX}" y="${y + 14}" text-anchor="middle" class="node-sub">${escapeHtml(thread.status || "active")}</text>
      </g>
    `);
    let previousX = threadX + 74;
    steps.forEach((step, idx) => {
      const x = 255 + idx * 170;
      lines.push(`<line x1="${previousX}" y1="${y}" x2="${x - 62}" y2="${y}" class="graph-edge ${escapeHtml(step.step_type || "user")}"></line>`);
      nodes.push(`
        <g class="graph-node step-node ${escapeHtml(step.step_type || "user")}">
          <rect x="${x - 62}" y="${y - 24}" width="124" height="48" rx="6"></rect>
          <text x="${x}" y="${y - 4}" text-anchor="middle">${escapeHtml(shortText(step.summary || "", 12))}</text>
          <text x="${x}" y="${y + 14}" text-anchor="middle" class="node-sub">${escapeHtml(stepTypeLabel(step.step_type))}</text>
        </g>
      `);
      previousX = x + 62;
    });
  });
  return `
    <div class="event-svg-wrap" data-graph-width="${width}" data-graph-height="${height}">
      <svg class="event-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="事件图谱">
        <g class="event-graph-viewport">
          ${lines.join("")}
          ${nodes.join("")}
        </g>
      </svg>
      <div class="event-graph-help">滚轮缩放，拖动画布平移，触摸可双指缩放。</div>
    </div>
  `;
}

function renderEventGraphRadialSvg(threads, stepsByThread) {
  const positions = new Map();
  const maxSteps = Math.max(1, ...threads.map((t) => (stepsByThread.get(String(t.id)) || []).length));
  const innerRadius = threads.length <= 1 ? 0 : Math.max(170, Math.min(440, threads.length * 22));
  const stepStart = threads.length <= 1 ? 190 : 165;
  const stepGap = 145;
  const margin = 140;
  const reach = innerRadius + stepStart + Math.max(0, maxSteps - 1) * stepGap + margin;
  const width = Math.max(820, reach * 2);
  const height = Math.max(620, reach * 2);
  const centerX = width / 2;
  const centerY = height / 2;
  const lines = [];
  const nodes = [];

  threads.forEach((thread, index) => {
    const angle = threads.length <= 1 ? 0 : -Math.PI / 2 + (Math.PI * 2 * index) / threads.length;
    const ux = Math.cos(angle);
    const uy = Math.sin(angle);
    const threadX = centerX + ux * innerRadius;
    const threadY = centerY + uy * innerRadius;
    positions.set(`thread-${thread.id}`, { x: threadX, y: threadY });
    const steps = stepsByThread.get(String(thread.id)) || [];
    steps.forEach((step, idx) => {
      positions.set(`step-${step.id}`, {
        x: threadX + ux * (stepStart + idx * stepGap),
        y: threadY + uy * (stepStart + idx * stepGap),
      });
    });
  });

  threads.forEach((thread) => {
    const threadPos = positions.get(`thread-${thread.id}`);
    if (!threadPos) return;
    const steps = stepsByThread.get(String(thread.id)) || [];
    const selectedClass = String(thread.id) === String(selectedEventThreadId) ? " selected" : "";
    nodes.push(`
      <g class="graph-node thread-node${selectedClass}">
        <rect x="${threadPos.x - 74}" y="${threadPos.y - 24}" width="148" height="48" rx="6"></rect>
        <text x="${threadPos.x}" y="${threadPos.y - 4}" text-anchor="middle">${escapeHtml(shortText(thread.title || "未命名事件", 12))}</text>
        <text x="${threadPos.x}" y="${threadPos.y + 14}" text-anchor="middle" class="node-sub">${escapeHtml(thread.status || "active")}</text>
      </g>
    `);
    let previous = { x: threadPos.x, y: threadPos.y };
    steps.forEach((step) => {
      const pos = positions.get(`step-${step.id}`);
      if (!pos) return;
      lines.push(`<line x1="${previous.x}" y1="${previous.y}" x2="${pos.x}" y2="${pos.y}" class="graph-edge ${escapeHtml(step.step_type || "user")}"></line>`);
      nodes.push(`
        <g class="graph-node step-node ${escapeHtml(step.step_type || "user")}">
          <rect x="${pos.x - 62}" y="${pos.y - 24}" width="124" height="48" rx="6"></rect>
          <text x="${pos.x}" y="${pos.y - 4}" text-anchor="middle">${escapeHtml(shortText(step.summary || "", 12))}</text>
          <text x="${pos.x}" y="${pos.y + 14}" text-anchor="middle" class="node-sub">${escapeHtml(stepTypeLabel(step.step_type))}</text>
        </g>
      `);
      previous = pos;
    });
  });

  return `
    <div class="event-svg-wrap" data-graph-width="${width}" data-graph-height="${height}">
      <svg class="event-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="事件图谱">
        <g class="event-graph-viewport">
          ${lines.join("")}
          ${nodes.join("")}
        </g>
      </svg>
      <div class="event-graph-help">滚轮缩放，拖动画布平移，触摸可双指缩放。</div>
    </div>
  `;
}

function setupEventGraphInteractions(root) {
  const wrap = root.querySelector(".event-svg-wrap");
  const svg = root.querySelector(".event-svg");
  const viewport = root.querySelector(".event-graph-viewport");
  if (!wrap || !svg || !viewport) return;
  const graphWidth = Number(wrap.dataset.graphWidth || 760);
  const graphHeight = Number(wrap.dataset.graphHeight || 260);
  const pointers = new Map();
  let panning = false;
  let lastPointer = null;
  let pinch = null;

  function applyTransform() {
    viewport.setAttribute(
      "transform",
      `translate(${eventGraphViewState.tx},${eventGraphViewState.ty}) scale(${eventGraphViewState.scale})`
    );
  }
  function fitInitialView() {
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const scale = Math.min(1.4, Math.max(0.25, Math.min((rect.width - 32) / graphWidth, (rect.height - 32) / graphHeight)));
    if (eventGraphViewState._graphWidth !== graphWidth || eventGraphViewState._graphHeight !== graphHeight) {
      eventGraphViewState = {
        scale,
        tx: (rect.width - graphWidth * scale) / 2,
        ty: 16,
        _graphWidth: graphWidth,
        _graphHeight: graphHeight,
      };
    }
    applyTransform();
  }
  function setPanning(active) {
    panning = active;
    svg.classList.toggle("is-panning", active);
  }
  function updatePinch() {
    const points = Array.from(pointers.values());
    if (points.length < 2) {
      pinch = null;
      return;
    }
    const a = points[0];
    const b = points[1];
    const cx = (a.x + b.x) / 2;
    const cy = (a.y + b.y) / 2;
    const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
    if (!pinch) {
      pinch = {
        cx,
        cy,
        distance,
        scale: eventGraphViewState.scale,
        tx: eventGraphViewState.tx,
        ty: eventGraphViewState.ty,
      };
      return;
    }
    const nextScale = Math.min(3.5, Math.max(0.2, pinch.scale * (distance / pinch.distance)));
    const graphX = (pinch.cx - pinch.tx) / pinch.scale;
    const graphY = (pinch.cy - pinch.ty) / pinch.scale;
    eventGraphViewState.scale = nextScale;
    eventGraphViewState.tx = cx - graphX * nextScale;
    eventGraphViewState.ty = cy - graphY * nextScale;
    applyTransform();
  }
  svg.addEventListener(
    "wheel",
    (ev) => {
      ev.preventDefault();
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const oldScale = eventGraphViewState.scale;
      const factor = ev.deltaY < 0 ? 1.12 : 0.89;
      eventGraphViewState.scale = Math.min(3.5, Math.max(0.2, eventGraphViewState.scale * factor));
      eventGraphViewState.tx = mx - ((mx - eventGraphViewState.tx) / oldScale) * eventGraphViewState.scale;
      eventGraphViewState.ty = my - ((my - eventGraphViewState.ty) / oldScale) * eventGraphViewState.scale;
      applyTransform();
    },
    { passive: false }
  );
  svg.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pointers.size >= 2) {
      setPanning(false);
      lastPointer = null;
      updatePinch();
    } else {
      setPanning(true);
      lastPointer = { x: ev.clientX, y: ev.clientY };
    }
    try {
      svg.setPointerCapture(ev.pointerId);
    } catch (e) {
      /* ignore */
    }
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!pointers.has(ev.pointerId)) return;
    ev.preventDefault();
    pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pointers.size >= 2) {
      updatePinch();
      return;
    }
    if (!panning || !lastPointer) return;
    eventGraphViewState.tx += ev.clientX - lastPointer.x;
    eventGraphViewState.ty += ev.clientY - lastPointer.y;
    lastPointer = { x: ev.clientX, y: ev.clientY };
    applyTransform();
  });
  function endPointer(ev) {
    pointers.delete(ev.pointerId);
    if (pointers.size >= 2) {
      updatePinch();
    } else if (pointers.size === 1) {
      const only = Array.from(pointers.values())[0];
      pinch = null;
      setPanning(true);
      lastPointer = { x: only.x, y: only.y };
    } else {
      pinch = null;
      setPanning(false);
      lastPointer = null;
    }
    try {
      svg.releasePointerCapture(ev.pointerId);
    } catch (e) {
      /* ignore */
    }
  }
  svg.addEventListener("pointerup", endPointer);
  svg.addEventListener("pointercancel", endPointer);
  svg.addEventListener("lostpointercapture", (ev) => {
    pointers.delete(ev.pointerId);
    if (!pointers.size) {
      pinch = null;
      setPanning(false);
      lastPointer = null;
    }
  });
  fitInitialView();
}

function renderEventTimeline(thread, steps) {
  if (!thread) return "<p class='hint'>请选择一条事件线。</p>";
  const stepHtml = steps.length
    ? steps
        .map(
          (step, idx) => `
          <div class="event-step">
            <div class="event-step-head">
              <span>${idx + 1}. ${escapeHtml(stepTypeLabel(step.step_type))}</span>
              <time>${escapeHtml(step.occurred_at || step.created_at || "")}</time>
            </div>
            <p>${escapeHtml(step.summary || "")}</p>
            ${step.cause ? `<p class="hint">触发：${escapeHtml(step.cause)}</p>` : ""}
            ${step.reflection ? `<p class="hint">反思：${escapeHtml(step.reflection)}</p>` : ""}
          </div>`
        )
        .join("")
    : "<p class='hint'>这条事件线还没有 step。</p>";
  return `
    <div class="event-detail">
      <h3>${escapeHtml(thread.title || "未命名事件")}</h3>
      <p class="hint">${escapeHtml(thread.source_event_key || "")}</p>
      ${thread.current_summary ? `<p>${escapeHtml(thread.current_summary)}</p>` : ""}
      ${thread.followup_hint ? `<p class="hint">跟进：${escapeHtml(thread.followup_hint)}</p>` : ""}
      <div class="event-timeline">${stepHtml}</div>
    </div>
  `;
}

function formatFacts(obj) {
  if (!obj || typeof obj !== "object") return "";
  return Object.entries(obj)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function parseFacts(text) {
  const out = {};
  for (const line of String(text).split("\n")) {
    const t = line.trim();
    if (!t) continue;
    const i = t.indexOf("=");
    if (i <= 0) continue;
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}

async function apiImportMemory(instanceId, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`/api/instances/${instanceId}/import_memory`, {
    method: "POST",
    body: fd,
  });
  const ct = r.headers.get("content-type") || "";
  let body;
  if (ct.includes("application/json")) {
    try {
      body = await r.json();
    } catch (e) {
      body = {};
    }
  } else {
    body = { detail: await r.text() };
  }
  if (!r.ok) {
    const d = body.detail;
    const errText = typeof d === "string" ? d : Array.isArray(d) ? JSON.stringify(d) : JSON.stringify(d || body);
    throw new Error(errText || r.statusText);
  }
  return body;
}

async function refreshRunStatus() {
  if (!selectedId) return;
  const st = await api(`/api/instances/${selectedId}`);
  const el = $("#run-status");
  if (el) el.textContent = st.running ? `运行中 pid=${st.pid}` : "未运行";
  const mp = $("#memory-path");
  if (mp && st.memory_path) mp.textContent = st.memory_path;
  const mex = $("#memory-exists");
  if (mex) {
    mex.textContent = st.memory_db_exists
      ? "当前已有记忆数据库文件。"
      : "尚未生成记忆文件（实例首次运行聊天后会创建 pupu.db）。仍可导入外部备份。";
  }
  const imb = $("#btn-import-memory");
  if (imb) {
    imb.disabled = !!st.running;
    imb.title = st.running ? "请先停止实例后再导入记忆" : "";
  }
}

async function saveSoulForm(id) {
  const ownersRaw = $("#f-owners").value.trim();
  const owner_ids = ownersRaw
    ? ownersRaw.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    : [];
  const allowedRaw = $("#f-private-allowed").value.trim();
  const private_allowed_ids = allowedRaw
    ? allowedRaw.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    : [];
  const cfg = {
    display_name: $("#f-display").value.trim(),
    port: Number($("#f-port").value),
    qq_mode: $("#f-qqmode").value,
    qq_app_id: $("#f-appid").value.trim(),
    qq_app_secret: $("#f-secret").value,
    owner_ids,
    private_reply_mode: $("#f-private-mode").value,
    private_allowed_ids,
  };
  const persona = {
    name: $("#f-name").value.trim(),
    core_persona: $("#f-core").value,
    seed_self_facts: parseFacts($("#f-facts").value),
  };
  await api(`/api/instances/${id}`, {
    method: "PUT",
    body: JSON.stringify({ instance: cfg, persona }),
  });
  alert("已保存");
  await loadInstances();
  await selectInstance(id);
}

async function openSoulsDialog() {
  const dlg = $("#dlg-souls");
  const panel = $("#souls-panel");
  const souls = await api("/api/souls");
  if (!souls.length) {
    panel.innerHTML = "<p class='hint'>暂无预设。在实例「灵魂」页可「另存为预设」。</p>";
  } else {
    panel.innerHTML = souls
      .map(
        (s) =>
          `<div class="soul-row" data-slug="${escapeHtml(s.slug)}">
            <span><strong>${escapeHtml(s.display_name)}</strong> <span class="hint">${escapeHtml(s.slug)}</span></span>
            <button type="button" class="danger btn-soul-del" data-slug="${escapeHtml(s.slug)}">删除</button>
          </div>`
      )
      .join("");
    panel.querySelectorAll(".btn-soul-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const slug = btn.dataset.slug;
        if (!window.confirm("删除预设 " + slug + "？")) return;
        await api(`/api/souls/${encodeURIComponent(slug)}`, { method: "DELETE" });
        await openSoulsDialog();
        await loadSoulsIntoSelect();
      });
    });
  }
  dlg.showModal();
}

$("#btn-souls-close").addEventListener("click", () => $("#dlg-souls").close());

$("#btn-new").addEventListener("click", async () => {
  await loadSoulsIntoSelect();
  $("#dlg-new").showModal();
});

$("#btn-new-cancel").addEventListener("click", () => $("#dlg-new").close());

$("#btn-souls").addEventListener("click", openSoulsDialog);

$("#btn-arbiter-start").addEventListener("click", async () => {
  try {
    await api("/api/arbiter/start", { method: "POST" });
    await refreshArbiter();
  } catch (e) {
    alert(String(e.message || e));
  }
});
$("#btn-arbiter-stop").addEventListener("click", async () => {
  try {
    await api("/api/arbiter/stop", { method: "POST" });
    await refreshArbiter();
  } catch (e) {
    alert(String(e.message || e));
  }
});

$("#form-new").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData($("#form-new"));
  const body = {
    display_name: fd.get("display_name") || "仆仆",
    qq_mode: fd.get("qq_mode") || "napcat",
  };
  const port = fd.get("port");
  if (port) body.port = Number(port);
  const ss = fd.get("soul_slug");
  if (ss) body.soul_slug = String(ss);
  const res = await api("/api/instances", { method: "POST", body: JSON.stringify(body) });
  $("#dlg-new").close();
  await loadInstances();
  await selectInstance(res.id);
});

async function init() {
  await loadSoulsIntoSelect();
  await loadInstances();
  await refreshArbiter();
  if (arbiterTimer) clearInterval(arbiterTimer);
  arbiterTimer = setInterval(refreshArbiter, 4000);
}

init().catch((err) => {
  console.error(err);
  document.body.insertAdjacentHTML(
    "beforeend",
    `<p style="color:#f85149;padding:1rem">加载失败: ${escapeHtml(String(err))}</p>`
  );
});
