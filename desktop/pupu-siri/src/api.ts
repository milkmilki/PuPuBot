import { invoke } from "@tauri-apps/api/core";

export type DesktopInstance = {
  id: string;
  display_name: string;
  port?: number;
  qq_mode?: string;
  running: boolean;
  pid?: number | null;
  runtime?: string | null;
};

export type DesktopStatus = {
  instances: DesktopInstance[];
  selected_instance_id: string;
  running: boolean;
  session_id: string;
};

export type DesktopEventName =
  | "desktop.connected"
  | "desktop.message"
  | "instance.status"
  | "chat.started"
  | "chat.reply_created"
  | "chat.error"
  | "memory.review_started"
  | "memory.review_finished";

export type DesktopEvent = {
  name: DesktopEventName | string;
  created_at: string;
  instance_id: string;
  payload: Record<string, unknown>;
};

export type DesktopChatResponse = {
  instance_id: string;
  session_id: string;
  reply: string;
};

export type ApiSecretStatus = {
  configured: boolean;
  preview: string;
  has_value?: boolean;
  masked?: string;
};

export type ApiKeySettings = {
  config_path: string;
  providers: string[];
  values: Record<string, string>;
  secrets: Record<string, ApiSecretStatus>;
};

export type McpToolSummary = {
  name: string;
  raw_name?: string;
  description?: string;
  exposures?: string[];
  admin_only?: boolean;
};

export type McpConfigField = {
  key: string;
  label?: string;
  type?: string;
  placeholder?: string;
  value?: string;
  secret?: ApiSecretStatus;
};

export type McpEnvField = {
  name: string;
  type?: string;
  value?: string;
  secret?: ApiSecretStatus;
};

export type McpServerSettings = {
  id: string;
  name?: string;
  display_name?: string;
  kind?: string;
  provider?: string;
  installed?: boolean;
  enabled?: boolean;
  loaded?: boolean;
  description?: string;
  command?: string;
  args?: string[];
  cwd?: string;
  timeout?: string;
  exposures?: string[];
  env?: McpEnvField[];
  tool_count?: number;
  tools?: McpToolSummary[];
  config_fields?: McpConfigField[];
  status?: string;
  error?: string;
  preset?: boolean;
};

export type McpPreset = {
  name: string;
  display_name?: string;
  description?: string;
  enabled?: boolean;
  command?: string;
  args?: string[];
  exposures?: string[];
  timeout?: number | string;
  env?: Record<string, string>;
};

export type McpSettings = {
  config_path: string;
  builtin_servers: McpServerSettings[];
  external_servers: McpServerSettings[];
  presets: McpPreset[];
};

export type McpEnvUpdate = {
  name: string;
  value: string;
};

export type McpExternalUpdate = {
  id: string;
  preset?: boolean;
  enabled: boolean;
  command: string;
  args: string[];
  cwd: string;
  timeout: string;
  exposures: string[];
  env: McpEnvUpdate[];
};

export type McpSettingsUpdate = {
  builtin_servers: Array<{ id: string; enabled: boolean }>;
  external_servers: McpExternalUpdate[];
  values: Record<string, string>;
  delete_external: string[];
};

export type McpTestResult = {
  ok: boolean;
  server_id: string;
  tools: McpToolSummary[];
  error: string;
};

export type ConsoleLaunchResponse = {
  status: "started" | "already_running" | string;
  message: string;
};

export class DesktopApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "DesktopApiError";
    this.status = status;
    this.detail = detail;
  }
}

export const CONSOLE_HTTP_BASE = (import.meta.env.VITE_PUPU_CONSOLE_URL ?? "http://127.0.0.1:8770").replace(
  /\/+$/,
  ""
);
export const CONSOLE_WS_BASE = CONSOLE_HTTP_BASE.replace(/^http/, "ws");

async function readError(response: Response): Promise<DesktopApiError> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail ?? body;
    return new DesktopApiError(response.status, typeof detail === "string" ? detail : `request ${response.status}`, detail);
  } catch {
    return new DesktopApiError(response.status, `request ${response.status}`);
  }
}

export async function fetchDesktopStatus(): Promise<DesktopStatus> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/status`);
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as DesktopStatus;
}

export async function sendDesktopChat(instanceId: string, text: string): Promise<DesktopChatResponse> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instance_id: instanceId, text })
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as DesktopChatResponse;
}

export async function fetchApiKeySettings(): Promise<ApiKeySettings> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/settings/api-keys`);
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as ApiKeySettings;
}

export async function saveApiKeySettings(values: Record<string, string>): Promise<ApiKeySettings> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/settings/api-keys`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values })
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as ApiKeySettings;
}

export async function fetchMcpSettings(): Promise<McpSettings> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/settings/mcp`);
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as McpSettings;
}

export async function saveMcpSettings(payload: McpSettingsUpdate): Promise<McpSettings> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/settings/mcp`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as McpSettings;
}

export async function refreshMcpSettings(): Promise<McpSettings> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/settings/mcp/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}"
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as McpSettings;
}

export async function testMcpServer(serverId: string): Promise<McpTestResult> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/settings/mcp/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ server_id: serverId })
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as McpTestResult;
}

export async function shutdownConsole(): Promise<{ ok: boolean; scheduled: boolean; message: string }> {
  const response = await fetch(`${CONSOLE_HTTP_BASE}/api/desktop/shutdown-console`, {
    method: "POST"
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as { ok: boolean; scheduled: boolean; message: string };
}

export async function startConsole(): Promise<ConsoleLaunchResponse> {
  return await invoke<ConsoleLaunchResponse>("start_console");
}

export async function stopConsole(): Promise<ConsoleLaunchResponse> {
  return await invoke<ConsoleLaunchResponse>("stop_console");
}

export function createDesktopEventSocket(): WebSocket {
  return new WebSocket(`${CONSOLE_WS_BASE}/ws/desktop/events`);
}
