# Agent Quick Reference

Fast lookup guide for AI agents working on this codebase. Read this before exploring files.

**Locale:** Code comments, prompts, and user-facing strings are often Chinese; this doc is English for agent handoff.

## Architecture Overview

```text
User (QQ / Terminal)
        |
        v
start.py / PuPu Console
        |
        v
InstanceActor per instance
  - CLI transport
  - NapCat OneBot v11 reverse WebSocket transport
  - MessageBuffer
  - scheduler / proactive / maintenance tasks
        |
        v
pupu.agent.chat()
  - SQLite memory
  - memU semantic cache
  - MCP/tools
  - batch review
```

PuPu is now instance-first and actor-only for runtime instances. There is no root-level default bot, no NoneBot plugin path, and no one-instance-one-Python-subprocess runtime. The console can host multiple `InstanceActor` objects in one Python process. The group arbiter remains a separate FastAPI service started by the console.

## Key Files

| Path | Purpose |
|------|---------|
| [start.py](../start.py) | Instance-first launcher: select/create an instance, then enter CLI or start NapCat QQ for that instance. |
| [pupu/actor/instance_actor.py](../pupu/actor/instance_actor.py) | Per-instance actor: owns context, transport, message buffer, scheduler, proactive and maintenance loops. |
| [pupu/actor/onebot_transport.py](../pupu/actor/onebot_transport.py) | Lightweight OneBot v11 reverse WebSocket server used by NapCat. |
| [pupu/actor/message_buffer.py](../pupu/actor/message_buffer.py) | Per-actor debounce, command interception, wait-followup sender registration, and open-group arbiter integration. |
| [pupu/command_service.py](../pupu/command_service.py) | Surface-agnostic command router used by CLI and NapCat actor. |
| [pupu/cli.py](../pupu/cli.py) | Terminal REPL backed by `InstanceActor`; uses `owner` as the session. |
| [pupu/agent.py](../pupu/agent.py) | `chat()` saves input, builds prompt, runs model/tool loop, saves output, schedules wait-followup, and triggers batch review. |
| [pupu/followup.py](../pupu/followup.py) | Dialogue JSON output protocol and parser. |
| [pupu/dialogue_loop.py](../pupu/dialogue_loop.py) | Wait-followup timers and sender registry. |
| [pupu/scheduler.py](../pupu/scheduler.py) | Due DB `scheduled_tasks` -> synthetic user message -> `chat(..., message_source="scheduled")` -> transport-neutral sender. |
| [pupu/proactive.py](../pupu/proactive.py) | Idle proactive message loop; actor supplies the owner sender. |
| [pupu/review_followups.py](../pupu/review_followups.py) | Batch review output parsing and persistence for summaries, person facts, event threads and tasks. |
| [pupu/storage/](../pupu/storage/) | SQLite schema and storage functions. SQLite is the fact source of truth. |
| [pupu/memory_index/](../pupu/memory_index/) | memU adapter and cache reconciliation. memU is a semantic cache over SQLite cards. |
| [pupu_console/process_manager.py](../pupu_console/process_manager.py) | Actor supervisor for the console; starts/stops instance actors in the console process. Also starts the external arbiter service. |
| [pupu_console/arbitrator.py](../pupu_console/arbitrator.py) | Open-group speaker arbitration. |
| [pupu_console/server.py](../pupu_console/server.py) | FastAPI console API. |

## Dialogue Output Protocol

Normal chat turns end the system prompt with `DIALOGUE_OUTPUT_PROTOCOL`:

```json
{"content": "...", "should_wait": true}
```

- `content`: text sent to the user.
- `should_wait`: if true and the session is eligible, `dialogue_loop.schedule_wait_timer()` starts a 180s in-memory timer.

Eligible sessions are `owner` and `private_<QQ>`. Group sessions never start wait-followup timers.

## One Chat Turn

1. CLI or NapCat transport creates an `ActorInboundMessage`.
2. `MessageBuffer` handles commands first. Any message beginning with `/` is never sent to the chat model.
3. Non-command private messages are debounced locally. Open-group messages go through the arbiter flow.
4. `chat()` saves the user message to SQLite with speaker metadata.
5. Prompt building loads recent messages, summaries, person facts, event threads and memU recall cards.
6. Model/tool loop runs.
7. Parsed assistant text is saved and sent through the active transport.
8. If enough chat messages accumulated, batch review writes summaries, person facts, event threads and scheduled task updates.

## Session Mapping

| Source | `context_session` | `identity_session` | Wait eligible |
|--------|-------------------|--------------------|---------------|
| CLI | `owner` | `owner` | Yes |
| NapCat private owner | `owner` | `owner` | Yes |
| NapCat private other QQ | `private_<QQ>` | `private_<QQ>` | Yes |
| NapCat group | `group_<group_id>` | `owner` or `private_<QQ>` by speaker | No |

`context_session` is where conversation history lives. `identity_session` is who the speaker is. In groups, the context is shared while each speaker keeps their own facts and familiarity identity.

## Instance Runtime

Every runtime action must happen under an `InstanceContext`.

Each instance directory contains:

- `instance.json`: display name, `qq_mode`, NapCat port, owner IDs, open groups, peer bot info, proactive flag, tool settings.
- `persona.json`: persona/soul data.
- `data/pupu.db`: SQLite source of truth.
- `data/memu.db`: memU semantic cache.
- `data/logs/`: per-instance logs.

NapCat configuration uses the port in `instance.json`:

```text
ws://127.0.0.1:<port>/onebot/v11/ws
```

No `.env.qq` file is generated for new instances.

## Memory Model

SQLite is authoritative:

- `messages`: conversation turns.
- `summaries`: batch review summaries.
- `person_facts`: person and relationship facts.
- `event_threads` / `event_steps`: event-chain memory.
- `people` / `event_people`: stable people and event participants.
- `scheduled_tasks`: scheduled reminders.

memU is a semantic cache. SQLite facts/events/summaries are rendered as natural-language cards and synced into memU for embedding recall. Tidy reconciles cache/source drift; it should not delete SQLite source rows.

## Open Groups

Open groups are configured in `instance.json.open_groups`. In those groups:

1. Every actor observes the group message.
2. Each actor posts observation data to the arbiter service.
3. The arbiter waits for group idle, selects one speaker or `none`, then exposes the decision through long-poll.
4. Only the selected actor replies.

`/silence on` locally stops the actor from observing/reconnecting to arbiter for that group. `/silence off` allows observing again.

## Common Tasks

| Goal | Where |
|------|-------|
| Change wait duration | `WAIT_DELAY_SECONDS` in [pupu/followup.py](../pupu/followup.py) |
| Change command behavior | [pupu/command_service.py](../pupu/command_service.py) and [pupu/command_registry.py](../pupu/command_registry.py) |
| Change NapCat transport | [pupu/actor/onebot_transport.py](../pupu/actor/onebot_transport.py) |
| Change message debounce / arbiter integration | [pupu/actor/message_buffer.py](../pupu/actor/message_buffer.py) |
| Change batch review | [pupu/agent.py](../pupu/agent.py), [pupu/review_followups.py](../pupu/review_followups.py), [pupu/persona/review_prompt.py](../pupu/persona/review_prompt.py) |
| Change memory schema | [pupu/storage/](../pupu/storage/) |
| Change console start/stop behavior | [pupu_console/process_manager.py](../pupu_console/process_manager.py) |

## Verification

Use the bundled environment:

```powershell
.\ForFun\Scripts\python.exe -m unittest discover tests
.\ForFun\Scripts\python.exe -m compileall -q pupu pupu_console tests
git diff --check
```

Do not run bare `unittest discover`; workspace root may contain unrelated files.

## Related Docs

- Chinese product overview: [README.md](../README.md)
- NapCat troubleshooting: [docs/QQ_BOT_TROUBLESHOOTING.md](QQ_BOT_TROUBLESHOOTING.md)
- Tavily MCP setup: [docs/TAVILY_MCP.md](TAVILY_MCP.md)
