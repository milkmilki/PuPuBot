# Agent Quick Reference

Fast-lookup guide for AI agents working on this codebase. Read this before exploring files.

## Architecture Overview

```
User (QQ / Terminal)
        │
        ▼
┌─────────────────┐     ┌──────────────────┐
│  Entry Points   │     │   NoneBot2       │
│  bot.py (QQ)    │────▶│   Framework      │
│  run.py (CLI)   │     │   (async event   │
└─────────────────┘     │    loop)         │
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │ plugins/         │
                        │ pupu_plugin.py   │
                        │ (routes QQ events│
                        │  to chat())      │
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │ pupu/agent.py    │
                        │ chat(input, sid) │
                        │ (core loop)      │
                        └──┬─────┬────┬────┘
                           │     │    │
              ┌────────────┘     │    └────────────┐
              ▼                  ▼                  ▼
     pupu/memory.py      pupu/persona.py     pupu/tools.py
     (SQLite CRUD)       (system prompts)    (registry facade)
```

## Key Files and What They Do

| File | Purpose | Key Functions |
|------|---------|---------------|
| `pupu/agent.py` | Core conversation loop | `chat(input, sid)` — main entry; `_judge_familiarity()` — scoring; `_extract_facts()` — fact extraction; `_maybe_summarize()` — auto-summarization |
| `pupu/memory.py` | SQLite persistence layer | `save_message()`, `get_recent_messages()`, `get/update_familiarity()`, `get_event_log()`, `upsert_user_facts()`, `get_user_facts()`, `save_summary()`, `get_summaries()` |
| `pupu/persona.py` | System prompt construction | `build_system_prompt(score, events, user_facts, summaries)`; prompt constants: `FAMILIARITY_JUDGE_PROMPT`, `FACT_EXTRACTION_PROMPT`, `SUMMARIZE_PROMPT` |
| `pupu/tools.py` | Tool registry facade | `TOOL_DEFINITIONS`, `execute_tool(name, input)`, `describe_tool_servers()` |
| `pupu/tooling/registry.py` | MCP-style registry + dispatch | `ToolRegistry`, `build_registry()`, `get_registry()` |
| `pupu/tooling/servers/*.py` | Builtin tool servers | `WEB_SERVER`, `FILESYSTEM_SERVER`, `SYSTEM_SERVER`, `MEDIA_SERVER`, `SCHEDULER_SERVER` |
| `pupu/cli.py` | Terminal interface | `main()` — REPL loop with Rich formatting |
| `plugins/pupu_plugin.py` | NoneBot QQ plugin | Handlers for OneBot v11 (private/group) and QQ Official (channel/c2c/group@) |
| `bot.py` | QQ bot entry point | Mode selection (napcat/official), NoneBot init, adapter registration |
| `config.json` | Runtime config | `qq_mode`, `qq_app_id`, `qq_app_secret`, `mode` (work/chat for Claude Code) |

## Data Flow: One Chat Turn

1. Message arrives → plugin extracts text, builds `session_id` (e.g. `private_12345`)
2. `chat(text, session_id)` called in `agent.py`
3. User message saved to SQLite (`messages` table)
4. Last 50 messages loaded as history
5. Familiarity score + last 20 events + user facts + summaries loaded
6. `build_system_prompt(score, events, user_facts, summaries)` constructs personality-aware system prompt with long-term memory
7. Claude API called with full history + system prompt + tool definitions
8. If tool_use response → execute tool → append results → call API again (loop)
9. Final text reply saved to SQLite
10. **Post-turn memory pipeline** (3 lightweight API calls):
    - `_judge_familiarity()` — evaluate familiarity score change
    - `_extract_facts()` — extract user facts (name, job, interests...) → `user_facts` table
    - `_maybe_summarize()` — if unsummarized messages > 80, compress oldest 60 into a summary → `summaries` table
11. Reply sent back to user

## Session ID Mapping

| Source | Format | Scope |
|--------|--------|-------|
| OneBot private | `private_{user_id}` | Per-user |
| OneBot group | `group_{group_id}` | Per-group (shared) |
| QQ Official channel | `channel_{channel_id}` | Per-channel |
| QQ Official C2C | `c2c_{user_openid}` | Per-user |
| QQ Official group | `qqgroup_{group_openid}` | Per-group |
| CLI | `default` | Single shared session |

## Database Schema (SQLite: `data/pupu.db`)

```sql
-- Conversation history (short-term: last 50 sent to API)
messages(id, session_id, role, content, timestamp)

-- Relationship score per session
familiarity(session_id PK, score 0-100, level, updated_at)

-- Score change events (last 20 injected into system prompt)
events(id, session_id, date, delta, description)

-- Long-term memory: extracted user facts (upserted per session+key)
user_facts(id, session_id, fact_key, fact_value, updated_at)
  UNIQUE INDEX on (session_id, fact_key)

-- Long-term memory: conversation summaries (auto-generated when history > 80 msgs)
summaries(id, session_id, summary, start_msg_id, end_msg_id, created_at)
```

## Familiarity System

Score 0-100, 5 levels with distinct personality prompts:

| Range | Level | Behavior |
|-------|-------|----------|
| 0-15 | 陌生 | Minimal responses, cold |
| 16-35 | 认识了 | Slightly warmer, occasional snarky comments |
| 36-60 | 熟了 | Actively chats, jokes, gives suggestions |
| 61-85 | 好朋友 | Very talkative, tsundere, shows care |
| 86-100 | 铁哥们 | Completely open, occasional playful vulnerability |

Score changes are judged by a separate API call (`_judge_familiarity`) after every turn. The judge returns a JSON array of `{delta, reason}` events.

## Long-Term Memory System

Three layers work together:

| Layer | Storage | Injected Into | Purpose |
|-------|---------|---------------|---------|
| Conversation history | `messages` table | `messages` array (last 50) | Recent context |
| User facts | `user_facts` table | System prompt "你对这个人的了解" | Remember who the user is (name, job, interests...) |
| Summaries | `summaries` table | System prompt "之前聊过的内容" | Remember what you talked about beyond the 50-msg window |
| Familiarity events | `events` table | System prompt "你们一起经历过的事" | Relationship milestones |

**Fact extraction**: After each turn, `_extract_facts()` sends the latest exchange to Claude and gets back a JSON dict of `{category: value}`. Facts are upserted (same key updates, not duplicates).

**Auto-summarization**: `_maybe_summarize()` checks if unsummarized messages exceed 80. If so, the oldest 60 are compressed into a ~200-char summary. Summaries accumulate over time (last 5 shown in prompt).

## Environment

- Python venv: `ForFun/` (activate: `source ForFun/Scripts/activate`)
- Platform: Windows 11, Git Bash
- API config in `.env`: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`
- NoneBot config in `.env.qq`: `HOST=0.0.0.0`, `PORT=8081`
- Model: `ppio/pa/claude-sonnet-4-6` (via PPIO proxy)

## Common Modification Patterns

**Add a new builtin tool:** add a `ToolSpec` to the right file under `pupu/tooling/servers/` (or create a new server module), then export that server from `pupu/tooling/servers/__init__.py`. `pupu/tools.py` is now only a compatibility facade.

**Prepare for real MCP/external services:** keep the same server boundary (`BaseToolServer`). A future stdio/HTTP MCP adapter only needs to expose `list_tools()` and bind each remote tool to a handler, then register that server in `ToolRegistry`.

**Change personality:** Edit prompts in `pupu/persona.py`. `CORE_PERSONA` is always included; `FAMILIARITY_PROMPTS[level]` is appended based on score.

**Add a new QQ command:** Add in `plugins/pupu_plugin.py` using `on_command()`. See `/score` and `/history` for examples.

**Change history window:** Modify the `50` in `get_recent_messages(50, session_id)` in `agent.py:57`.

**Tune summarization:** `SUMMARY_THRESHOLD` (default 80) and `SUMMARY_BATCH_SIZE` (default 60) in `agent.py`. Lower threshold = summarize more often.

**Add a new data table:** Add CREATE TABLE in `memory.py:init_db()`, add CRUD functions in the same file.
