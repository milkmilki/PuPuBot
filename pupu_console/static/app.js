/* global fetch, WebSocket, document, window */

const $ = (sel) => document.querySelector(sel);

let selectedId = null;
let logTimer = null;
let arbiterTimer = null;
let ws = null;

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
  `;
  $("#f-qqmode").value = data.qq_mode || "napcat";

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
  const cfg = {
    display_name: $("#f-display").value.trim(),
    port: Number($("#f-port").value),
    qq_mode: $("#f-qqmode").value,
    qq_app_id: $("#f-appid").value.trim(),
    qq_app_secret: $("#f-secret").value,
    owner_ids,
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
