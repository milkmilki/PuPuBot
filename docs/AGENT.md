# Agent Quick Reference

Fast-lookup guide for AI agents working on this codebase. Read this before exploring files.

**Locale:** Code comments, prompts, and user-facing strings are often Chinese; this doc is English for agent handoff.

## Architecture Overview

```
User (QQ / Terminal)
        │
        ▼
┌──────────────────┐        start.py selects/creates an instance
│  start.py        │──────────────────────────────────┐
│  Windows .bat    │                                  │
└──────────────────┘                                  │
                                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  CLI: pupu/cli.py  │  QQ: plugins/pupu_plugin.py                 │
│  → pupu.agent.chat │  → plugins/pupu_support/buffering.py       │
│                    │     (debounce → asyncio.to_thread(chat))    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │  pupu/agent.py       │
                     │  chat(user, sid, …)  │
                     └───┬──────────────┬───┘
                         │              │
         ┌───────────────┘              └────────────────┐
         ▼                                               ▼
  pupu/followup.py                          pupu/dialogue_loop.py
  (JSON output protocol + parse)            (180s wait timer + sender registry)
  pupu/persona/ (build_system_prompt)       pupu/followup_manager.py (Timer)
  pupu/memory.py → pupu/storage/*           plugins register senders (QQ/CLI)
  pupu/tooling/* (tools)
```

## Key Files and What They Do

| Path | Purpose |
|------|---------|
| [start.py](../start.py) | Instance-first launcher: select/create `instances/<id>`, then start CLI or QQ for that instance. No root-level default bot. |
| [pupu/instance_main.py](../pupu/instance_main.py) | Subprocess entry for one instance; loads `pupu.yaml`, then reads `instance.json` + generated per-instance `.env.qq` / DB / persona |
| [pupu/agent.py](../pupu/agent.py) | **`chat()`** — save user msg, build prompt, `chat_complete` + tools, `_parse_dialogue_output`, save assistant; start-of-turn **`cancel_wait_timer`**; end **`schedule_wait_timer`** if `should_wait`; **`message_source`** (`chat`, `scheduled`, `wait_followup`, …); batch review when `source == chat` |
| [pupu/followup.py](../pupu/followup.py) | **`DIALOGUE_OUTPUT_PROTOCOL`** (sole spec for chat JSON output); **`WAIT_DELAY_SECONDS = 180`**; **`_parse_dialogue_output`** → `(content, should_wait)` with JSON repair / heuristics |
| [pupu/message_sources.py](../pupu/message_sources.py) | Single source for persisted **`message_source`** / `source` strings: **`CHAT`**, **`SCHEDULED`**, **`PROACTIVE`**, **`WAIT_FOLLOWUP`** (agent still exports **`REVIEW_SOURCE = CHAT`**) |
| [pupu/sessions.py](../pupu/sessions.py) | **`OWNER_SESSION`** — canonical owner session id (`"owner"`) for CLI, NapCat owner mapping, proactive, scheduler, seed data |
| [pupu/dialogue_loop.py](../pupu/dialogue_loop.py) | **`schedule_wait_timer` / `cancel_wait_timer` / `has_wait_timer`**; **`register_sender(sid, fn)`** — sync `fn(text)` delivers assistant text when timer fires; **`_on_timer_fire`** calls `chat(..., message_source=WAIT_FOLLOWUP)` then sender |
| [pupu/followup_manager.py](../pupu/followup_manager.py) | Per-session `threading.Timer` + worker queue (used by `dialogue_loop`) |
| [pupu/proactive.py](../pupu/proactive.py) | Idle proactive messages; generation uses **`system + DIALOGUE_OUTPUT_PROTOCOL`**, parses output; **`schedule_wait_timer(OWNER_SESSION)`** if `should_wait`; cancels pending wait timer before generating |
| [pupu/review_followups.py](../pupu/review_followups.py) | Batch review outputs: summaries, familiarity delta, facts, important events, scheduled task updates |
| [pupu/scheduler.py](../pupu/scheduler.py) | Due **DB** `scheduled_tasks` → synthetic user message → `chat(..., message_source="scheduled")` → send via OneBot / CLI print; **`_is_wait_followup_task`** filters legacy `wait_followup*` DB rows (real wait is in-memory now) |
| [pupu/cli.py](../pupu/cli.py) | Terminal REPL; imports **`OWNER_SESSION`** from [`sessions.py`](../pupu/sessions.py); **`register_sender(OWNER_SESSION, ...)`** so timer follow-ups print in-terminal |
| [plugins/pupu_plugin.py](../plugins/pupu_plugin.py) | NoneBot plugin load entry |
| [plugins/pupu_support/buffering.py](../plugins/pupu_support/buffering.py) | Debounced QQ messages; **every** inbound message **`cancel_wait_timer(sid)`**; for eligible sessions **`register_sender`** (private OneBot delivery via `run_coroutine_threadsafe`). **Open-group** sessions push every message to `/api/observe` and react to decisions via `arbiter_decision_subscriber`; private/owner sessions still use the local `debounce_flush`. |
| [pupu_console/arbitrator.py](../pupu_console/arbitrator.py) | Group speaker arbitration. `observe()` records each group message (deduped on `(group_id, message_id)`) and upserts the reporter into `group_bots`. `run_judge()` runs **once per debounce flush** under a per-group threading lock and writes to `group_decisions`. `await_decision_async()` is the long-poll backing `/api/await_decision` (uses `asyncio.Event`). Legacy `arbitrate()` (`/api/group_arbitrate`) kept for the deprecation window with the same per-group lock. |
| [pupu_console/arbiter_server.py](../pupu_console/arbiter_server.py) | FastAPI app for the arbiter. Routes: `POST /api/observe`, `GET /api/await_decision`, `POST /api/group_arbitrate` (legacy), `GET /health`. Owns the per-group debounce watchdog (idle reset + hard cap). |
| [plugins/pupu_support/onebot_handlers.py](../plugins/pupu_support/onebot_handlers.py) | OneBot v11 private/group; on connect **`register_owner_wait_followup_sender`** so proactive/timer can reach owner without a recent user turn |
| [pupu/memory.py](../pupu/memory.py) | Facade re-exporting [pupu/storage/*](../pupu/storage/) (messages, familiarity, facts, summaries, important events, scheduled tasks, …) |
| [pupu/persona/](../pupu/persona/) | **`build_system_prompt`** (persona + memory + scheduler tool rules only; **no** duplicate JSON format block — that lives in `followup.DIALOGUE_OUTPUT_PROTOCOL`) |

## Dialogue output protocol (every normal model turn)

The chat system prompt ends with **`DIALOGUE_OUTPUT_PROTOCOL`** ([followup.py](../pupu/followup.py)): the model must return **only** JSON `{"content": "...", "should_wait": true|false}`.

- **`content`**: text shown to the user.
- **`should_wait`**: if `true` and the session is **eligible**, [`dialogue_loop.schedule_wait_timer`](../pupu/dialogue_loop.py) starts a **180s** in-memory timer. If the user does not start a new `chat()` turn in time, **`_on_timer_fire`** runs another `chat()` with a synthetic system reminder and `message_source="wait_followup"`, then pushes **`content`** through the registered **sender**.

**Eligibility:** only **`owner`** and **`private_<digits>`** (OneBot private QQ id). **Group / channel / c2c_* sessions** still parse JSON but **do not** start the wait timer (avoids awkward group pings).

**Cancellation:** `chat()` **always** calls `cancel_wait_timer(session_id)` at the **start** of a turn (covers CLI and scheduled/wait_followup turns). QQ **`buffer_message`** also cancels on every inbound user message before debounce.

**Unlimited follow-up chains:** each wait_followup reply may again set `should_wait=true` and schedule another 180s (no cap in code).

## Data Flow: One Chat Turn (simplified)

1. Ingress builds **`session_id`** (see table below) → **`chat(text, session_id, …)`**.
2. **`cancel_wait_timer(session_id)`**.
3. User message saved to SQLite (`messages`), with optional **`source=message_source`**.
4. Recent history (**`CHAT_HISTORY_LIMIT = 30`**), familiarity, facts, summaries, important events loaded; **`build_system_prompt`** + **`DIALOGUE_OUTPUT_PROTOCOL`**.
5. Model + tool loop via **`chat_complete`**.
6. **`_parse_dialogue_output`** → `final_text`, `should_wait`; assistant message saved.
7. If **`should_wait`**: **`schedule_wait_timer`** (if eligible); batch review may run when **`message_source == REVIEW_SOURCE`** (`"chat"`).
8. Return **`final_text`** to caller (CLI prints; QQ sends segments).

**Batch review** (not per-message judge calls): after enough **`chat`** turns or idle time, **`_maybe_batch_review`** runs a structured review (summary, familiarity delta, facts, important events, task updates). Tunables: **`REVIEW_INTERVAL`**, **`REVIEW_IDLE_SECONDS`** in [agent.py](../pupu/agent.py).

## Session ID Mapping

| Source | `session_id` | Wait timer eligible? |
|--------|----------------|----------------------|
| OneBot private, configured owner | `owner` | Yes |
| OneBot private, other QQ | `private_{qq}` (digits) | Yes |
| OneBot group | `group_{group_id}` | No |
| QQ Official channel | `channel_{id}` | No |
| QQ Official C2C | `c2c_{openid}` | No (not `private_*`) |
| QQ Official group @ | `qqgroup_{group_openid}` | No |
| Terminal CLI | `owner` ([cli.py](../pupu/cli.py)) | Yes |

## Database (SQLite: `instances/<id>/data/pupu.db`)

High-signal tables (not exhaustive):

- **`messages`** — conversation; columns include role, content, session_id, timestamps / sources as implemented in storage.
- **`familiarity`**, **`events`** — score 0–100 and relationship events.
- **`user_facts`**, **`self_facts`** — long-term facts.
- **`summaries`**, **`important_events`** — compression and notable episodes.
- **`scheduled_tasks`** — user/assistant scheduled reminders; legacy rows titled `wait_followup*` are ignored by the scheduler loop; **live** wait-followup uses **memory timers** only.

## Session model

Runtime code separates the old single `session_id` concept into two meanings while keeping the SQLite schema unchanged:

- **Context session** — where the conversation happens. `messages`, `summaries`, pending review cursors, and scheduled-task delivery use this. Private chats use `owner` / `private_<QQ>`; groups use `group_<群号>`.
- **Identity session** — who the speaker is. `familiarity`, legacy familiarity `events`, `user_facts`, `self_facts`, and `important_events` use this. Owner maps to `owner`; other users map to `private_<QQ>`.

In normal private chat these two values are identical. In open groups, `context_session=group_<群号>` and `identity_session=owner|private_<QQ>`, so group context stays shared while each person keeps their own score and facts.

## Familiarity (overview)

Score **0–100** with tiered persona text in **`pupu/persona/`** (see `FAMILIARITY_PROMPTS` / `builder.py`). Proactive messaging respects **`PROACTIVE_THRESHOLD`** in [proactive.py](../pupu/proactive.py) / [familiarity.py](../pupu/familiarity.py).

## Environment & run

- Venv: **`ForFun/`** (Windows: `ForFun\Scripts\python.exe`; `启动仆仆.bat` and `启动仆仆控制台.bat` are double-click wrappers).
- Global config: **`pupu.yaml`** — provider keys, owner QQ ids, NapCat port, Console / arbiter ports, memU, web search, and TTS settings. `pupu.app_config.apply_app_config_env()` maps YAML keys into the `PUPU_*` environment variables consumed by runtime modules.
- QQ runtime file: **`instances/<id>/.env.qq`** — generated from `pupu.yaml` for NoneBot host/port; NapCat reverse WS to `ws://127.0.0.1:<port>/onebot/v11/ws`.

## Instances

Every PuPu runtime uses an instance. Several PuPu processes can run in parallel. Each instance = **own subprocess**, SQLite DB, `instance.json`, `persona.json`, and generated `.env.qq`.

| Path | Role |
|------|------|
| [pupu/instance_main.py](../pupu/instance_main.py) | `python -m pupu.instance_main --dir <dir>` — sets `PUPU_INSTANCE_DIR`, `PUPU_CONFIG_PATH`, `PUPU_DB_PATH`, `PUPU_PERSONA_PATH`; **cwd** = repo root so `plugins/` loads; `qq_mode` in `instance.json`: `napcat` \| `official` \| `cli`. |
| [pupu_console/](../pupu_console/) | Web UI: `python -m pupu_console` — CRUD `instances/`, CRUD `souls/` presets, start/stop, logs / WS console, **memory path + SQLite import** (replace `instances/<id>/data/pupu.db` when stopped). APIs: `GET /api/instances/{id}/memory_path`, `POST /api/instances/{id}/import_memory` (multipart file). |
| `instances/<id>/` | Runtime files for one bot; chat memory DB: `instances/<id>/data/pupu.db` (also exposed as `memory_path` on `GET /api/instances/{id}`). |
| `souls/<slug>.json` | Preset soul (persona + `tool_servers` only at instance level). **Apply** does not change `port`, `qq_app_id`, `qq_app_secret`, or `owner_ids`. |

**Memory import (覆盖):** Validates SQLite header + `messages` table; if `pupu.db` already exists, copies to `pupu.db.bak.<timestamp>.<token>` before replace. Import only when the subprocess is stopped (UI/API return 409 if `running`).

**Per-process env:** `PUPU_DB_PATH`, `PUPU_CONFIG_PATH`, `PUPU_PERSONA_PATH` (see [pupu/config.py](../pupu/config.py), [pupu/persona/core.py](../pupu/persona/core.py), [pupu/storage/db.py](../pupu/storage/db.py)); `PUPU_INSTANCE_DIR` sends logs to that instance’s `data/logs/` ([logging_utils.py](../pupu/logging_utils.py)). Tests may set `PUPU_REPO_ROOT` for [pupu_console/paths.py](../pupu_console/paths.py).

Shared prompt modules (`FAMILIARITY_PROMPTS`, proactive, batch review) stay in code unless you extend per-instance loading.

## Common modification patterns

| Goal | Where |
|------|--------|
| Change wait duration | `WAIT_DELAY_SECONDS` in [followup.py](../pupu/followup.py) (and timer uses it via [dialogue_loop.py](../pupu/dialogue_loop.py)) |
| Change who gets wait timers | `is_followup_eligible()` in [dialogue_loop.py](../pupu/dialogue_loop.py) |
| Change follow-up synthetic prompt | `_on_timer_fire()` in [dialogue_loop.py](../pupu/dialogue_loop.py) |
| QQ: ensure owner receives timer sends without prior message | `register_owner_wait_followup_sender` in [onebot_handlers.py](../plugins/pupu_support/onebot_handlers.py) |
| Dialogue JSON rules / parsing | [followup.py](../pupu/followup.py) |
| Batch review behavior | [agent.py](../pupu/agent.py) `_maybe_batch_review*`, [review_followups.py](../pupu/review_followups.py) |
| New tool server | `pupu/tooling/servers/*`, register in `__init__.py`; enable in instance `instance.json` → `tool_servers` |
| Persona tone | `pupu/persona/*.py` |
| Import / replace instance SQLite memory | Web console “运行” tab or `POST /api/instances/{id}/import_memory`; logic in [instance_store.py](../pupu_console/instance_store.py) (`replace_memory_db`) |

## Related docs

- Chinese product overview: [README.md](../README.md)
- QQ troubleshooting: [docs/QQ_BOT_TROUBLESHOOTING.md](QQ_BOT_TROUBLESHOOTING.md)
