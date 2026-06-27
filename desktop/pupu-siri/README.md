# PuPu Siri

PuPu Siri is the first Tauri desktop pet shell for PuPuBot. It lives in this repo while the desktop API is still changing, but it is intentionally isolated so it can be split into a separate project later.

## Boundary

- This project talks to PuPu Console only through HTTP and WebSocket.
- It does not import Python code or call PuPu agent internals.
- The backend-side integration remains in PuPu Console as a thin desktop adapter.
- The fixed desktop chat session is `desktop_owner`.

## API Contract

| Surface | Purpose |
| --- | --- |
| `GET /api/desktop/status` | Read instances, selected instance, running state, and desktop session id. |
| `POST /api/desktop/chat` | Send one desktop chat message to a running instance. Returns `409` when the instance is not running. |
| `GET /api/desktop/settings/api-keys` / `PUT /api/desktop/settings/api-keys` | Read and save model provider settings. Secret fields are masked on read. |
| `GET /api/desktop/settings/mcp` / `PUT /api/desktop/settings/mcp` | Read and save builtin tool and external stdio MCP settings. Secret fields are masked on read. |
| `POST /api/desktop/settings/mcp/refresh` | Refresh PuPu Console's MCP/tool registry after settings changes. |
| `POST /api/desktop/settings/mcp/test` | Test one builtin or external MCP server and return a sanitized tool summary. |
| `WS /ws/desktop/events` | Receive forwarded hook events for instance status, chat lifecycle, and memory review state. |

The TypeScript client contract is kept in `src/api.ts`. Keep new backend fields additive so older desktop builds can keep running against newer Console builds.

## Settings UI

Right-click the Siri orb and choose `设置` to open the local settings panel.
The panel has two tabs:

- `模型`: model providers and API keys.
- `MCP`: builtin tools such as media/vision and external stdio MCP servers such as Tavily.

The MCP tab reuses PuPu Console's MCP settings API. It can toggle builtin tools, edit Vision settings, add/remove external MCP servers, test one server, and refresh the tool registry. Empty secret inputs preserve the existing value.

## Development

On Windows, you can start both PuPu Console and PuPu Siri from the repository root:

```powershell
.\启动pupu_siri.bat
```

The launcher checks backend dependencies, installs desktop dependencies when `node_modules` is missing, then starts PuPu Console and Tauri dev mode in the background.
Launcher logs are written to `logs/launcher/`, and the expanded PuPu Siri panel has an exit button for closing the desktop pet.
It prefers `pnpm`, falls back to `corepack pnpm`, and can use `npm` when neither pnpm nor corepack is available. Node.js and Rust/Cargo still need to be installed and available in `PATH`.

Manual startup is still useful when debugging. Start PuPu Console first:

```powershell
python -m pupu_console
```

Create or start an instance at `http://127.0.0.1:8770`, then run the desktop app:

```powershell
cd desktop\pupu-siri
pnpm install
pnpm run dev
```

By default the desktop app connects to `http://127.0.0.1:8770`. Override it with `VITE_PUPU_CONSOLE_URL` when needed.

## Current Scope

V1 intentionally excludes automatic Console startup, instance creation, Live2D, skins, voice wakeup, global hotkeys, and auto-update. Those belong in later slices once the local desktop entry is stable.
