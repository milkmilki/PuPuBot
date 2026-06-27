import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  DesktopApiError,
  type ApiKeySettings,
  type DesktopEvent,
  type DesktopStatus,
  type McpEnvField,
  type McpExternalUpdate,
  type McpServerSettings,
  type McpSettings,
  createDesktopEventSocket,
  fetchMcpSettings,
  fetchApiKeySettings,
  fetchDesktopStatus,
  refreshMcpSettings as refreshMcpSettingsApi,
  saveApiKeySettings,
  saveMcpSettings as saveMcpSettingsApi,
  sendDesktopChat,
  startConsole,
  stopConsole,
  testMcpServer as testMcpServerApi
} from "./api";
import "./styles.css";

type PetState = "idle" | "thinking" | "speaking" | "error" | "reviewing" | "offline";
const COLLAPSED_SIZE = new LogicalSize(112, 112);
const EXPANDED_SIZE = new LogicalSize(384, 520);
const MENU_SIZE = new LogicalSize(220, 352);
const STREAM_CHUNK_DELAY_MS = 58;
const SIZE_DELTA = {
  width: EXPANDED_SIZE.width - COLLAPSED_SIZE.width,
  height: EXPANDED_SIZE.height - COLLAPSED_SIZE.height
};

const app = document.querySelector<HTMLElement>("#app")!;
const orb = document.querySelector<HTMLButtonElement>("#orb")!;
const panel = document.querySelector<HTMLElement>("#panel")!;
const panelHead = document.querySelector<HTMLElement>("#panelHead")!;
const contextMenu = document.querySelector<HTMLElement>("#contextMenu")!;
const togglePanelButton = document.querySelector<HTMLButtonElement>("#togglePanelButton")!;
const settingsButton = document.querySelector<HTMLButtonElement>("#settingsButton")!;
const mcpSettingsButton = document.querySelector<HTMLButtonElement>("#mcpSettingsButton")!;
const openConsoleButton = document.querySelector<HTMLButtonElement>("#openConsoleButton")!;
const startConsoleButton = document.querySelector<HTMLButtonElement>("#startConsoleButton")!;
const shutdownConsoleButton = document.querySelector<HTMLButtonElement>("#shutdownConsoleButton")!;
const exitButton = document.querySelector<HTMLButtonElement>("#exitButton")!;
const collapseButton = document.querySelector<HTMLButtonElement>("#collapseButton")!;
const settingsPanel = document.querySelector<HTMLElement>("#settingsPanel")!;
const settingsCloseButton = document.querySelector<HTMLButtonElement>("#settingsCloseButton")!;
const settingsModelTab = document.querySelector<HTMLButtonElement>("#settingsModelTab")!;
const settingsMcpTab = document.querySelector<HTMLButtonElement>("#settingsMcpTab")!;
const settingsForm = document.querySelector<HTMLFormElement>("#settingsForm")!;
const settingsPath = document.querySelector<HTMLElement>("#settingsPath")!;
const settingsStatus = document.querySelector<HTMLElement>("#settingsStatus")!;
const settingsSaveButton = document.querySelector<HTMLButtonElement>("#settingsSaveButton")!;
const mcpSettingsPanel = document.querySelector<HTMLElement>("#mcpSettingsPanel")!;
const mcpSettingsList = document.querySelector<HTMLElement>("#mcpSettingsList")!;
const mcpSettingsStatus = document.querySelector<HTMLElement>("#mcpSettingsStatus")!;
const mcpSaveButton = document.querySelector<HTMLButtonElement>("#mcpSaveButton")!;
const mcpRefreshButton = document.querySelector<HTMLButtonElement>("#mcpRefreshButton")!;
const mcpAddButton = document.querySelector<HTMLButtonElement>("#mcpAddButton")!;
const mcpAddTavilyButton = document.querySelector<HTMLButtonElement>("#mcpAddTavilyButton")!;
const statusText = document.querySelector<HTMLElement>("#statusText")!;
const instanceText = document.querySelector<HTMLElement>("#instanceText")!;
const messages = document.querySelector<HTMLElement>("#messages")!;
const chatForm = document.querySelector<HTMLFormElement>("#chatForm")!;
const chatInput = document.querySelector<HTMLInputElement>("#chatInput")!;
const sendButton = document.querySelector<HTMLButtonElement>("#sendButton")!;

type TauriWindow = ReturnType<typeof getCurrentWindow>;

function resolveAppWindow(): TauriWindow | null {
  try {
    return getCurrentWindow();
  } catch {
    return null;
  }
}

const appWindow = resolveAppWindow();

let selectedInstanceId = "";
let expanded = false;
let settingsOpen = false;
let lastState: PetState = "offline";
let stateBeforeReview: PetState | null = null;
let ws: WebSocket | null = null;
let reconnectTimer = 0;
let dragStart: { x: number; y: number } | null = null;
let didDrag = false;
let menuOpenFromCollapsed = false;
let streamRunId = 0;
let consoleShutdownRequested = false;
let shutdownConfirmArmed = false;
let shutdownConfirmTimer = 0;
let settingsView: "model" | "mcp" = "model";
let mcpState: McpSettings | null = null;
let deletedExternalMcp: string[] = [];

function setState(state: PetState, label?: string) {
  lastState = state;
  app.className = `pet ${state}${expanded || settingsOpen ? " expanded" : ""}`;
  statusText.textContent = label ?? statusLabel(state);
}

function statusLabel(state: PetState): string {
  switch (state) {
    case "idle":
      return "Ready";
    case "thinking":
      return "Thinking";
    case "speaking":
      return "Replying";
    case "reviewing":
      return "Reviewing memory";
    case "error":
      return "Needs attention";
    default:
      return "Offline";
  }
}

function resetShutdownConfirmation() {
  window.clearTimeout(shutdownConfirmTimer);
  shutdownConfirmTimer = 0;
  shutdownConfirmArmed = false;
  shutdownConsoleButton.textContent = "关闭 Console";
  shutdownConsoleButton.title = "";
}

function armShutdownConfirmation() {
  window.clearTimeout(shutdownConfirmTimer);
  shutdownConfirmArmed = true;
  shutdownConsoleButton.textContent = "确认关闭 Console";
  shutdownConsoleButton.title = "会停止 8770 控制台和它管理的运行中实例";
  shutdownConfirmTimer = window.setTimeout(resetShutdownConfirmation, 5000);
}

async function resizeWindow(value: boolean, currentExpanded = expanded) {
  if (!appWindow) return;
  try {
    if (value === currentExpanded) {
      await appWindow.setSize(value ? EXPANDED_SIZE : COLLAPSED_SIZE);
      return;
    }
    const currentPosition = await appWindow.outerPosition();
    const scaleFactor = await appWindow.scaleFactor();
    const direction = value ? -1 : 1;
    await appWindow.setSize(value ? EXPANDED_SIZE : COLLAPSED_SIZE);
    await appWindow.setPosition(
      new LogicalPosition(
        currentPosition.x / scaleFactor,
        currentPosition.y / scaleFactor + (SIZE_DELTA.height * direction)
      )
    );
  } catch {
    // Window resizing only works inside Tauri; keep the UI usable in web preview.
  }
}

async function resizeForContextMenu(open: boolean) {
  if (!appWindow) return;
  if (expanded) return;
  try {
    const currentPosition = await appWindow.outerPosition();
    const scaleFactor = await appWindow.scaleFactor();
    const direction = open ? -1 : 1;
    const heightDelta = MENU_SIZE.height - COLLAPSED_SIZE.height;
    await appWindow.setSize(open ? MENU_SIZE : COLLAPSED_SIZE);
    await appWindow.setPosition(
      new LogicalPosition(
        currentPosition.x / scaleFactor,
        currentPosition.y / scaleFactor + heightDelta * direction
      )
    );
  } catch {
    // Context menu resizing only applies inside Tauri.
  }
}

function scrollMessagesToBottom() {
  messages.scrollTop = messages.scrollHeight;
}

function providerLabel(provider: string) {
  switch (provider) {
    case "anthropic":
      return "Anthropic";
    case "deepseek":
      return "DeepSeek";
    case "xiaoshuoai":
      return "小说 AI";
    default:
      return provider;
  }
}

function settingControls() {
  return Array.from(
    settingsForm.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-setting-key]")
  );
}

function fillProviderSelect(select: HTMLSelectElement, providers: string[], value: string) {
  select.textContent = "";
  for (const provider of providers) {
    const option = document.createElement("option");
    option.value = provider;
    option.textContent = providerLabel(provider);
    select.appendChild(option);
  }
  select.value = value || providers[0] || "";
}

function applySettingsToForm(settings: ApiKeySettings) {
  settingsPath.textContent = settings.config_path;
  const providers = settings.providers.length ? settings.providers : ["deepseek", "anthropic", "xiaoshuoai"];
  const providerSelects = settingsForm.querySelectorAll<HTMLSelectElement>("select[data-setting-key]");
  providerSelects.forEach((select) => {
    const key = select.dataset.settingKey || "";
    fillProviderSelect(select, providers, settings.values[key] || settings.values["llm.provider"] || "deepseek");
  });
  for (const control of settingControls()) {
    const key = control.dataset.settingKey || "";
    if (control instanceof HTMLSelectElement) continue;
    const secret = settings.secrets[key];
    if (secret) {
      control.value = "";
      control.placeholder = secret.configured ? `${secret.preview}（留空保持不变）` : "API key";
    } else {
      control.value = settings.values[key] || "";
    }
  }
}

async function loadSettings() {
  settingsStatus.textContent = "正在读取设置...";
  settingsSaveButton.disabled = true;
  try {
    applySettingsToForm(await fetchApiKeySettings());
    settingsStatus.textContent = "留空的 API key 不会覆盖已有值。";
  } catch (error) {
    settingsStatus.textContent = error instanceof Error ? error.message : "读取设置失败。";
  } finally {
    settingsSaveButton.disabled = false;
  }
}

async function saveSettings() {
  const values: Record<string, string> = {};
  for (const control of settingControls()) {
    const key = control.dataset.settingKey || "";
    if (!key) continue;
    const value = control.value.trim();
    if (control instanceof HTMLInputElement && control.type === "password" && !value) continue;
    values[key] = value;
  }
  settingsStatus.textContent = "正在保存...";
  settingsSaveButton.disabled = true;
  try {
    applySettingsToForm(await saveApiKeySettings(values));
    settingsStatus.textContent = "已保存到 pupu.yaml；运行中的实例可能需要重启后完全生效。";
  } catch (error) {
    settingsStatus.textContent = error instanceof Error ? error.message : "保存失败。";
  } finally {
    settingsSaveButton.disabled = false;
  }
}

function setSettingsView(view: "model" | "mcp") {
  settingsView = view;
  settingsModelTab.classList.toggle("active", view === "model");
  settingsMcpTab.classList.toggle("active", view === "mcp");
  settingsModelTab.setAttribute("aria-selected", view === "model" ? "true" : "false");
  settingsMcpTab.setAttribute("aria-selected", view === "mcp" ? "true" : "false");
  settingsForm.hidden = view !== "model";
  mcpSettingsPanel.hidden = view !== "mcp";
}

function splitList(value: string) {
  return value
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function makeElement<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string
) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function makeTextInput(value = "", placeholder = "", type = "text") {
  const input = document.createElement("input");
  input.type = type;
  input.value = value;
  input.placeholder = placeholder;
  input.autocomplete = "off";
  return input;
}

function appendField(
  parent: HTMLElement,
  labelText: string,
  input: HTMLInputElement,
  hint?: string
) {
  const label = makeElement("label", "settings-field", labelText);
  label.appendChild(input);
  if (hint) {
    label.appendChild(makeElement("span", "field-hint", hint));
  }
  parent.appendChild(label);
}

function secretHint(secret?: { configured?: boolean; has_value?: boolean; preview?: string; masked?: string }) {
  if (!secret || (!secret.configured && !secret.has_value)) return "";
  const preview = secret.masked || secret.preview || "";
  return preview ? `已配置 ${preview}` : "已配置";
}

function serverStatusText(server: McpServerSettings) {
  if (!server.enabled) return "disabled";
  if (server.loaded) return "loaded";
  return server.status || "configured";
}

function toolNames(server: McpServerSettings) {
  const tools = server.tools || [];
  if (!tools.length) return "暂无已加载工具";
  return tools
    .map((tool) => tool.name || tool.raw_name || "")
    .filter(Boolean)
    .join(", ");
}

function renderBuiltinMcpCard(server: McpServerSettings) {
  const card = makeElement("article", "mcp-card");
  card.dataset.mcpKind = "builtin";
  card.dataset.mcpId = server.id;

  const head = makeElement("header", "mcp-card-head");
  const titleBlock = makeElement("div");
  titleBlock.appendChild(makeElement("h4", "", server.display_name || server.name || server.id));
  const badges = makeElement("div", "mcp-badges");
  badges.appendChild(makeElement("span", "badge", "内置"));
  badges.appendChild(makeElement("span", server.loaded ? "badge ok" : "badge", serverStatusText(server)));
  badges.appendChild(makeElement("span", "badge", `${Number(server.tool_count || 0)} tools`));
  titleBlock.appendChild(badges);
  head.appendChild(titleBlock);

  const enabledLabel = makeElement("label", "inline-check", "启用");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = !!server.enabled;
  enabled.dataset.mcpEnabled = "1";
  enabledLabel.prepend(enabled);
  head.appendChild(enabledLabel);
  card.appendChild(head);

  if (server.description) card.appendChild(makeElement("p", "mcp-desc", server.description));
  card.appendChild(makeElement("p", "mcp-tools", toolNames(server)));

  const fields = makeElement("div", "mcp-fields");
  for (const field of server.config_fields || []) {
    const input = makeTextInput(
      field.value || "",
      field.placeholder || "",
      field.type === "secret" ? "password" : field.type === "number" ? "number" : "text"
    );
    input.dataset.mcpValueKey = field.key;
    appendField(fields, field.label || field.key, input, secretHint(field.secret));
  }
  if (fields.childElementCount) card.appendChild(fields);

  const actions = makeElement("div", "mcp-actions");
  const testButton = makeElement("button", "settings-secondary", "测试");
  testButton.type = "button";
  testButton.addEventListener("click", () => void testMcpServer(server.id, card));
  actions.appendChild(testButton);
  card.appendChild(actions);
  card.appendChild(makeElement("p", "mcp-card-status"));
  return card;
}

function renderEnvField(field?: McpEnvField) {
  const row = makeElement("div", "mcp-env-row");
  const name = makeTextInput(field?.name || "API_KEY", "ENV_NAME");
  name.dataset.mcpEnvKey = "1";
  const value = makeTextInput("", field?.type === "secret" ? "留空保留旧值" : "value", field?.type === "secret" ? "password" : "text");
  value.dataset.mcpEnvValue = "1";
  const hint = secretHint(field?.secret);
  row.appendChild(name);
  row.appendChild(value);
  if (hint) row.appendChild(makeElement("span", "field-hint", hint));
  return row;
}

function renderExternalMcpCard(server: McpServerSettings) {
  const card = makeElement("article", "mcp-card");
  card.dataset.mcpKind = "external";
  card.dataset.mcpId = server.id;
  if (server.preset) card.dataset.mcpPreset = "1";

  const head = makeElement("header", "mcp-card-head");
  const titleBlock = makeElement("div");
  titleBlock.appendChild(makeElement("h4", "", server.display_name || server.name || server.id || "新的外接 MCP"));
  const badges = makeElement("div", "mcp-badges");
  badges.appendChild(makeElement("span", "badge", "外接"));
  badges.appendChild(makeElement("span", server.loaded ? "badge ok" : "badge", serverStatusText(server)));
  badges.appendChild(makeElement("span", "badge", `${Number(server.tool_count || 0)} tools`));
  titleBlock.appendChild(badges);
  head.appendChild(titleBlock);

  const enabledLabel = makeElement("label", "inline-check", "启用");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = !!server.enabled;
  enabled.dataset.mcpEnabled = "1";
  enabledLabel.prepend(enabled);
  head.appendChild(enabledLabel);
  card.appendChild(head);

  if (server.description) card.appendChild(makeElement("p", "mcp-desc", server.description));
  card.appendChild(makeElement("p", "mcp-tools", toolNames(server)));

  const fields = makeElement("div", "mcp-fields");
  const nameInput = makeTextInput(server.id || "", "server name");
  nameInput.dataset.mcpExternalName = "1";
  if (server.preset) nameInput.readOnly = true;
  appendField(fields, "名称", nameInput);
  const exposuresInput = makeTextInput((server.exposures || []).join(","), "chat,proactive");
  exposuresInput.dataset.mcpExternalExposures = "1";
  appendField(fields, "Exposures", exposuresInput);
  const commandInput = makeTextInput(server.command || "", "cmd / npx / uvx");
  commandInput.dataset.mcpExternalCommand = "1";
  appendField(fields, "Command", commandInput);
  const argsInput = makeTextInput((server.args || []).join("\n"), "每行或逗号分隔");
  argsInput.dataset.mcpExternalArgs = "1";
  appendField(fields, "Args", argsInput);
  const cwdInput = makeTextInput(server.cwd || "", "工作目录，可留空");
  cwdInput.dataset.mcpExternalCwd = "1";
  appendField(fields, "CWD", cwdInput);
  const timeoutInput = makeTextInput(String(server.timeout || ""), "30", "number");
  timeoutInput.dataset.mcpExternalTimeout = "1";
  appendField(fields, "Timeout", timeoutInput);

  const envSection = makeElement("div", "mcp-env-section");
  envSection.appendChild(makeElement("p", "mcp-env-title", "Env"));
  const envList = makeElement("div", "mcp-env-list");
  const envFields = server.env?.length ? server.env : [{ name: "API_KEY", type: "secret", value: "" }];
  for (const field of envFields) {
    envList.appendChild(renderEnvField(field));
  }
  envSection.appendChild(envList);
  fields.appendChild(envSection);
  card.appendChild(fields);

  const actions = makeElement("div", "mcp-actions");
  const testButton = makeElement("button", "settings-secondary", "测试");
  testButton.type = "button";
  testButton.addEventListener("click", () => {
    const serverId = nameInput.value.trim() || server.id;
    void testMcpServer(serverId, card);
  });
  const deleteButton = makeElement("button", "settings-secondary danger-button", "删除");
  deleteButton.type = "button";
  deleteButton.addEventListener("click", () => deleteExternalMcp(server.id, card));
  actions.appendChild(testButton);
  actions.appendChild(deleteButton);
  card.appendChild(actions);
  card.appendChild(makeElement("p", "mcp-card-status"));
  return card;
}

function renderMcpSettings(settings: McpSettings) {
  mcpState = settings;
  deletedExternalMcp = [];
  settingsPath.textContent = settings.config_path;
  mcpSettingsList.textContent = "";

  const builtinSection = makeElement("section", "mcp-section");
  builtinSection.appendChild(makeElement("h3", "", "内置工具"));
  for (const server of settings.builtin_servers || []) {
    builtinSection.appendChild(renderBuiltinMcpCard(server));
  }
  mcpSettingsList.appendChild(builtinSection);

  const externalSection = makeElement("section", "mcp-section");
  externalSection.appendChild(makeElement("h3", "", "外接 MCP"));
  const external = settings.external_servers || [];
  if (!external.length) {
    externalSection.appendChild(makeElement("p", "settings-status", "没有外接 MCP。"));
  }
  for (const server of external) {
    externalSection.appendChild(renderExternalMcpCard(server));
  }
  mcpSettingsList.appendChild(externalSection);
}

async function loadMcpSettings() {
  mcpSettingsStatus.textContent = "正在读取 MCP 设置...";
  mcpSaveButton.disabled = true;
  try {
    renderMcpSettings(await fetchMcpSettings());
    mcpSettingsStatus.textContent = "Secret 留空会保留原值。";
  } catch (error) {
    mcpSettingsStatus.textContent = error instanceof Error ? error.message : "读取 MCP 设置失败。";
  } finally {
    mcpSaveButton.disabled = false;
  }
}

function collectMcpPayload() {
  const builtin_servers: Array<{ id: string; enabled: boolean }> = [];
  const external_servers: McpExternalUpdate[] = [];
  const values: Record<string, string> = {};

  mcpSettingsList.querySelectorAll<HTMLElement>("[data-mcp-kind='builtin']").forEach((card) => {
    const id = card.dataset.mcpId || "";
    if (!id) return;
    builtin_servers.push({
      id,
      enabled: !!card.querySelector<HTMLInputElement>("[data-mcp-enabled]")?.checked
    });
    card.querySelectorAll<HTMLInputElement>("[data-mcp-value-key]").forEach((input) => {
      const key = input.dataset.mcpValueKey || "";
      const value = input.value.trim();
      if (!key) return;
      if (input.type === "password" && !value) return;
      if (value || input.type !== "password") values[key] = value;
    });
  });

  mcpSettingsList.querySelectorAll<HTMLElement>("[data-mcp-kind='external']").forEach((card) => {
    if (card.dataset.deleted === "1") return;
    const id = card.querySelector<HTMLInputElement>("[data-mcp-external-name]")?.value.trim() || "";
    if (!id) return;
    const env: Array<{ name: string; value: string }> = [];
    card.querySelectorAll<HTMLElement>(".mcp-env-row").forEach((row) => {
      const name = row.querySelector<HTMLInputElement>("[data-mcp-env-key]")?.value.trim() || "";
      const value = row.querySelector<HTMLInputElement>("[data-mcp-env-value]")?.value.trim() || "";
      if (name && value) env.push({ name, value });
    });
    external_servers.push({
      id,
      preset: card.dataset.mcpPreset === "1",
      enabled: !!card.querySelector<HTMLInputElement>("[data-mcp-enabled]")?.checked,
      command: card.querySelector<HTMLInputElement>("[data-mcp-external-command]")?.value.trim() || "",
      args: splitList(card.querySelector<HTMLInputElement>("[data-mcp-external-args]")?.value || ""),
      cwd: card.querySelector<HTMLInputElement>("[data-mcp-external-cwd]")?.value.trim() || "",
      timeout: card.querySelector<HTMLInputElement>("[data-mcp-external-timeout]")?.value.trim() || "",
      exposures: splitList(card.querySelector<HTMLInputElement>("[data-mcp-external-exposures]")?.value || "chat"),
      env
    });
  });

  return { builtin_servers, external_servers, values, delete_external: deletedExternalMcp };
}

async function saveMcpSettings() {
  mcpSettingsStatus.textContent = "正在保存 MCP 设置...";
  mcpSaveButton.disabled = true;
  try {
    renderMcpSettings(await saveMcpSettingsApi(collectMcpPayload()));
    renderMcpSettings(await refreshMcpSettingsApi());
    mcpSettingsStatus.textContent = "MCP 设置已保存并刷新。";
  } catch (error) {
    mcpSettingsStatus.textContent = error instanceof Error ? error.message : "保存 MCP 设置失败。";
  } finally {
    mcpSaveButton.disabled = false;
  }
}

async function refreshMcpSettings() {
  mcpSettingsStatus.textContent = "正在刷新工具...";
  mcpRefreshButton.disabled = true;
  try {
    renderMcpSettings(await refreshMcpSettingsApi());
    mcpSettingsStatus.textContent = "工具已刷新。";
  } catch (error) {
    mcpSettingsStatus.textContent = error instanceof Error ? error.message : "刷新工具失败。";
  } finally {
    mcpRefreshButton.disabled = false;
  }
}

async function testMcpServer(serverId: string, card: HTMLElement) {
  const status = card.querySelector<HTMLElement>(".mcp-card-status");
  if (status) status.textContent = "测试中...";
  try {
    const result = await testMcpServerApi(serverId);
    if (status) {
      status.textContent = result.ok
        ? `可用，发现 ${result.tools?.length || 0} 个工具。`
        : `失败：${result.error || "unknown error"}`;
    }
  } catch (error) {
    if (status) status.textContent = error instanceof Error ? error.message : "测试失败。";
  }
}

function deleteExternalMcp(serverId: string, card: HTMLElement) {
  const name = serverId || card.querySelector<HTMLInputElement>("[data-mcp-external-name]")?.value.trim() || "new";
  if (name !== "new") deletedExternalMcp.push(name);
  card.dataset.deleted = "1";
  card.hidden = true;
  mcpSettingsStatus.textContent = `已标记删除 ${name}，保存后生效。`;
}

function addExternalMcpCard() {
  const sections = Array.from(mcpSettingsList.querySelectorAll<HTMLElement>(".mcp-section"));
  const section = sections[sections.length - 1];
  if (!section) return;
  section.appendChild(
    renderExternalMcpCard({
      id: "",
      display_name: "新的外接 MCP",
      enabled: false,
      status: "new",
      command: "",
      args: [],
      exposures: ["chat"],
      env: [],
      tools: []
    })
  );
}

function addTavilyPresetCard() {
  if (mcpSettingsList.querySelector("[data-mcp-kind='external'][data-mcp-id='tavily']:not([data-deleted='1'])")) {
    mcpSettingsStatus.textContent = "Tavily 已经在列表里。";
    return;
  }
  const preset = mcpState?.presets?.find((item) => item.name === "tavily");
  const sections = Array.from(mcpSettingsList.querySelectorAll<HTMLElement>(".mcp-section"));
  const section = sections[sections.length - 1];
  if (!section) return;
  section.appendChild(
    renderExternalMcpCard({
      id: "tavily",
      display_name: preset?.display_name || "Web Search / Tavily",
      description: preset?.description || "",
      enabled: !!preset?.enabled,
      status: "preset",
      preset: true,
      command: preset?.command || "cmd",
      args: preset?.args || ["/c", "npx", "-y", "tavily-mcp@latest"],
      timeout: String(preset?.timeout || 30),
      exposures: preset?.exposures || ["chat", "proactive"],
      env: Object.keys(preset?.env || { TAVILY_API_KEY: "" }).map((name) => ({
        name,
        type: name.includes("KEY") || name.includes("TOKEN") ? "secret" : "text",
        value: ""
      })),
      tools: []
    })
  );
}

async function ensureWindowVisible() {
  if (!appWindow) return;
  try {
    const monitor = await primaryMonitor();
    if (!monitor) return;
    const position = await appWindow.outerPosition();
    const scaleFactor = monitor.scaleFactor || 1;
    const x = position.x / scaleFactor;
    const y = position.y / scaleFactor;
    const workPosition = monitor.workArea.position.toLogical(scaleFactor);
    const workSize = monitor.workArea.size.toLogical(scaleFactor);
    const minX = workPosition.x - 32;
    const minY = workPosition.y - 32;
    const maxX = workPosition.x + workSize.width - 32;
    const maxY = workPosition.y + workSize.height - 32;
    if (x < minX || y < minY || x > maxX || y > maxY) {
      await appWindow.setPosition(
        new LogicalPosition(workPosition.x + 120, workPosition.y + 120)
      );
    }
  } catch {
    // Keep startup resilient in web preview or if monitor queries are unavailable.
  }
}

async function setExpanded(value: boolean) {
  closeContextMenu();
  const wasExpanded = expanded || settingsOpen;
  if (value && settingsOpen) {
    settingsOpen = false;
    settingsPanel.hidden = true;
  }
  if (value) {
    await resizeWindow(true, wasExpanded);
  }
  expanded = value;
  app.classList.toggle("expanded", expanded || settingsOpen);
  panel.toggleAttribute("hidden", !expanded);
  if (expanded) {
    window.setTimeout(() => {
      scrollMessagesToBottom();
      chatInput.focus();
    }, 40);
  } else {
    chatInput.blur();
    await resizeWindow(false, wasExpanded);
  }
}

async function setSettingsOpen(value: boolean, initialView: "model" | "mcp" = "model") {
  closeContextMenu(false);
  const wasExpanded = expanded || settingsOpen;
  if (value) {
    await resizeWindow(true, wasExpanded);
    setSettingsView(initialView);
  }
  settingsOpen = value;
  if (value) {
    expanded = false;
    panel.hidden = true;
  }
  settingsPanel.toggleAttribute("hidden", !settingsOpen);
  app.classList.toggle("expanded", expanded || settingsOpen);
  if (settingsOpen) {
    if (settingsView === "mcp") {
      await loadMcpSettings();
      mcpSaveButton.focus();
    } else {
      await loadSettings();
      settingsSaveButton.focus();
    }
  } else if (!expanded) {
    await resizeWindow(false, wasExpanded);
  }
}

function addMessage(role: "user" | "assistant" | "system", text: string) {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.textContent = text;
  messages.appendChild(item);
  scrollMessagesToBottom();
}

function createMessage(role: "user" | "assistant" | "system", text = "") {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.textContent = text;
  messages.appendChild(item);
  scrollMessagesToBottom();
  return item;
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function playPseudoStream(text: string) {
  const runId = ++streamRunId;
  const item = createMessage("assistant");
  setState("speaking");
  const content = text || "(empty reply)";
  for (const char of Array.from(content)) {
    if (runId !== streamRunId) return;
    item.textContent += char;
    scrollMessagesToBottom();
    const delay = /[。！？.!?]/.test(char)
      ? STREAM_CHUNK_DELAY_MS * 7
      : /[，、；：,;:]/.test(char)
        ? STREAM_CHUNK_DELAY_MS * 4
        : STREAM_CHUNK_DELAY_MS;
    await sleep(delay);
  }
  if (runId === streamRunId) {
    setState("idle");
  }
}

function selectBestInstance(status: DesktopStatus) {
  const running = status.instances.find((item) => item.running);
  const selected =
    status.instances.find((item) => item.id === status.selected_instance_id) ??
    running ??
    status.instances[0];
  selectedInstanceId = selected?.id ?? "";
  instanceText.textContent = selected
    ? `${selected.display_name || selected.id}${selected.running ? "" : " (stopped)"}`
    : "No PuPu instance";
  setState(running ? "idle" : "offline");
}

async function refreshStatus() {
  try {
    const status = await fetchDesktopStatus();
    selectBestInstance(status);
    return true;
  } catch (error) {
    selectedInstanceId = "";
    instanceText.textContent = "PuPu Console unavailable";
    setState("offline");
    return false;
  }
}

async function sendChat(text: string) {
  if (!selectedInstanceId) {
    addMessage("system", "PuPu Console is not ready.");
    setState("offline");
    return;
  }
  addMessage("user", text);
  setState("thinking");
  sendButton.disabled = true;
  chatInput.disabled = true;
  try {
    const body = await sendDesktopChat(selectedInstanceId, text);
    await playPseudoStream(body.reply || "(empty reply)");
  } catch (error) {
    streamRunId += 1;
    if (error instanceof DesktopApiError && error.status === 409) {
      await refreshStatus();
    }
    addMessage("system", error instanceof Error ? error.message : "Chat failed.");
    setState("error");
  } finally {
    sendButton.disabled = false;
    chatInput.disabled = false;
    chatInput.focus();
  }
}

function handleDesktopEvent(event: DesktopEvent) {
  if (event.name === "desktop.message") {
    if (event.instance_id && selectedInstanceId && event.instance_id !== selectedInstanceId) return;
    const text = String(event.payload.text ?? "").trim();
    if (text) void playPseudoStream(text);
    return;
  }
  if (event.name === "instance.status") {
    const status = String(event.payload.status ?? "");
    if (event.instance_id) selectedInstanceId = event.instance_id;
    if (status === "running") {
      setState("idle");
      void refreshStatus();
    } else if (status === "starting") {
      setState("thinking", "Starting");
    } else if (status === "failed") {
      setState("error");
    } else if (status === "stopped" || status === "stopping") {
      setState("offline");
      void refreshStatus();
    }
    return;
  }
  if (event.name === "chat.started") {
    setState("thinking");
    return;
  }
  if (event.name === "chat.reply_created") {
    if (!sendButton.disabled) setState("speaking");
    return;
  }
  if (event.name === "chat.error") {
    setState("error");
    return;
  }
  if (event.name === "memory.review_started") {
    if (lastState !== "reviewing") {
      stateBeforeReview = lastState;
    }
    setState("reviewing");
    return;
  }
  if (event.name === "memory.review_finished") {
    if (lastState !== "reviewing") return;
    const nextState = stateBeforeReview === "offline" || stateBeforeReview === "error" ? stateBeforeReview : "idle";
    stateBeforeReview = null;
    setState(nextState);
  }
}

function connectEvents() {
  window.clearTimeout(reconnectTimer);
  ws?.close();
  ws = createDesktopEventSocket();
  ws.onopen = () => {
    if (lastState === "offline") void refreshStatus();
  };
  ws.onmessage = (message) => {
    try {
      handleDesktopEvent(JSON.parse(String(message.data)) as DesktopEvent);
    } catch {
      // Ignore malformed local events.
    }
  };
  ws.onclose = () => {
    setState("offline");
    if (consoleShutdownRequested) return;
    reconnectTimer = window.setTimeout(connectEvents, 2000);
  };
  ws.onerror = () => {
    setState("offline");
  };
}

async function exitApp() {
  streamRunId += 1;
  window.clearTimeout(reconnectTimer);
  ws?.close();
  try {
    if (appWindow) {
      await appWindow.close();
    } else {
      window.close();
    }
  } catch {
    window.close();
  }
}

async function openConsole() {
  try {
    await openUrl("http://127.0.0.1:8770");
  } catch {
    window.open("http://127.0.0.1:8770", "_blank", "noopener");
  }
}

async function waitForConsoleReady() {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await sleep(750);
    if (await refreshStatus()) return true;
  }
  return false;
}

async function startConsoleFromMenu() {
  closeContextMenu();
  consoleShutdownRequested = false;
  window.clearTimeout(reconnectTimer);
  setState("thinking", "Starting Console");
  try {
    const result = await startConsole();
    addMessage("system", result.message || "PuPu Console is starting.");
    const ready = await waitForConsoleReady();
    if (ready) {
      connectEvents();
      setState("idle");
    } else {
      setState("offline", "Console starting");
      addMessage("system", "Console 已启动，但 8770 还没有响应；稍等几秒后可再次打开控制页。");
    }
  } catch (error) {
    addMessage("system", error instanceof Error ? error.message : "启动 Console 失败。");
    setState("error");
  }
}

async function shutdownConsoleFromMenu() {
  closeContextMenu();
  resetShutdownConfirmation();
  consoleShutdownRequested = true;
  streamRunId += 1;
  window.clearTimeout(reconnectTimer);
  try {
    const result = await stopConsole();
    addMessage("system", result.message || "正在关闭 PuPu Console...");
    setState("offline", "Console closing");
    ws?.close();
  } catch (error) {
    consoleShutdownRequested = false;
    addMessage("system", error instanceof Error ? error.message : "关闭 Console 失败。");
    setState("error");
    connectEvents();
  }
}

async function openContextMenu(event: MouseEvent) {
  event.preventDefault();
  event.stopPropagation();
  if (settingsOpen || !contextMenu.hidden) {
    return;
  }
  if (!expanded) {
    menuOpenFromCollapsed = true;
    await resizeForContextMenu(true);
  } else {
    menuOpenFromCollapsed = false;
  }
  togglePanelButton.textContent = expanded ? "收起面板" : "展开面板";
  contextMenu.hidden = false;
}

function closeContextMenu(resize = true) {
  resetShutdownConfirmation();
  if (contextMenu.hidden) {
    if (!resize) menuOpenFromCollapsed = false;
    return;
  }
  contextMenu.hidden = true;
  if (resize && menuOpenFromCollapsed) {
    menuOpenFromCollapsed = false;
    void resizeForContextMenu(false);
  } else if (!resize) {
    menuOpenFromCollapsed = false;
  }
}

function canStartWindowDrag(event: MouseEvent) {
  if (event.button !== 0) return false;
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest("#orb")) return true;
  return !target?.closest("button, input, textarea, select, a, .context-menu, .settings-panel");
}

function beginWindowDrag(event: MouseEvent) {
  if (!canStartWindowDrag(event)) return;
  dragStart = { x: event.screenX, y: event.screenY };
}

orb.addEventListener("click", () => {
  if (didDrag) {
    didDrag = false;
    return;
  }
  void setExpanded(!expanded);
});
orb.addEventListener("mousedown", (event) => {
  beginWindowDrag(event);
});
orb.addEventListener("contextmenu", (event) => {
  void openContextMenu(event);
});
window.addEventListener("contextmenu", (event) => {
  if (!settingsOpen && contextMenu.hidden) return;
  event.preventDefault();
  event.stopPropagation();
});
panelHead.addEventListener("mousedown", (event) => {
  beginWindowDrag(event);
});
window.addEventListener("mousemove", async (event) => {
  if (!dragStart) return;
  if (!appWindow) return;
  const dx = event.screenX - dragStart.x;
  const dy = event.screenY - dragStart.y;
  if (Math.hypot(dx, dy) < 4) return;
  didDrag = true;
  dragStart = null;
  await appWindow.startDragging();
});
window.addEventListener("mouseup", () => {
  dragStart = null;
  if (didDrag) {
    window.setTimeout(() => {
      didDrag = false;
    }, 250);
  }
});
window.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest(".context-menu")) return;
  closeContextMenu();
});
window.addEventListener("blur", () => {
  dragStart = null;
  closeContextMenu();
});
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (settingsOpen) {
    void setSettingsOpen(false);
  } else {
    closeContextMenu();
  }
});
collapseButton.addEventListener("click", () => {
  void setExpanded(false);
});
exitButton.addEventListener("click", () => {
  void exitApp();
});
togglePanelButton.addEventListener("click", () => {
  void setExpanded(!expanded);
});
settingsButton.addEventListener("click", () => {
  void setSettingsOpen(true, "model");
});
mcpSettingsButton.addEventListener("click", () => {
  void setSettingsOpen(true, "mcp");
});
settingsModelTab.addEventListener("click", () => {
  setSettingsView("model");
  void loadSettings();
});
settingsMcpTab.addEventListener("click", () => {
  setSettingsView("mcp");
  if (!mcpState) void loadMcpSettings();
});
openConsoleButton.addEventListener("click", () => {
  closeContextMenu();
  void openConsole();
});
startConsoleButton.addEventListener("click", () => {
  void startConsoleFromMenu();
});
shutdownConsoleButton.addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  if (!shutdownConfirmArmed) {
    armShutdownConfirmation();
    return;
  }
  void shutdownConsoleFromMenu();
});
settingsCloseButton.addEventListener("click", () => {
  void setSettingsOpen(false);
});
settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void saveSettings();
});
mcpSaveButton.addEventListener("click", () => {
  void saveMcpSettings();
});
mcpRefreshButton.addEventListener("click", () => {
  void refreshMcpSettings();
});
mcpAddButton.addEventListener("click", () => {
  addExternalMcpCard();
});
mcpAddTavilyButton.addEventListener("click", () => {
  addTavilyPresetCard();
});
chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;
  chatInput.value = "";
  void sendChat(text);
});

panel.hidden = true;
void (async () => {
  await resizeWindow(false);
  await ensureWindowVisible();
})();
void refreshStatus();
connectEvents();
