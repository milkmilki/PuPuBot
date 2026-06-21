"""Shared group-chat speaker arbitration for multiple PuPu actors.

The process-local :mod:`pupu.arbiter_runtime` observes group messages, debounces
quiet windows, calls ``run_judge`` and wakes actor waiters directly. Persistent
state lives in ``instances/_shared/arbiter.db``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import instances_dir

_DECISION_TTL_SECONDS = 300

# Per-group threading lock for the decision critical section so two debounce
# tasks cannot race on ``recent_speakers`` / decision writes for the same group.
# NOTE: Process-local only. The actor runtime runs one embedded arbiter per
# console process.
_GROUP_LOCKS: dict[str, threading.Lock] = {}
_GROUP_LOCKS_META = threading.Lock()
_JUDGE_ERROR_LOG_LOCK = threading.Lock()


def _group_lock(group_id: str) -> threading.Lock:
    with _GROUP_LOCKS_META:
        lock = _GROUP_LOCKS.get(group_id)
        if lock is None:
            lock = threading.Lock()
            _GROUP_LOCKS[group_id] = lock
        return lock


# Per-group asyncio.Event used to wake up ``await_decision_async`` long-pollers
# the moment a new decision is committed. Stored as ``(loop, event)`` so we
# never reuse an Event from a different loop. Replaced atomically after every
# decision so future waiters block on a fresh Event instead of seeing the old
# already-set state.
_GROUP_EVENTS: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
_GROUP_EVENTS_META = threading.Lock()


def _ensure_group_event(group_id: str) -> asyncio.Event:
    """Return (creating if necessary) the asyncio.Event for ``group_id``.

    Must be called from inside the embedded arbiter runtime's running event loop.
    """
    loop = asyncio.get_running_loop()
    with _GROUP_EVENTS_META:
        existing = _GROUP_EVENTS.get(group_id)
        if existing and existing[0] is loop:
            return existing[1]
        event = asyncio.Event()
        _GROUP_EVENTS[group_id] = (loop, event)
        return event


def _signal_group_event(group_id: str) -> None:
    """Set the existing event (waking up waiters) and rotate it.

    Called from a worker thread (run_judge) via ``loop.call_soon_threadsafe``.
    """
    with _GROUP_EVENTS_META:
        existing = _GROUP_EVENTS.get(group_id)
        if not existing:
            return
        loop, event = existing
        new_event = asyncio.Event()
        _GROUP_EVENTS[group_id] = (loop, new_event)
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # Loop already closed; nothing to wake up.
        pass


def _db_path() -> Path:
    root = instances_dir() / "_shared"
    root.mkdir(parents=True, exist_ok=True)
    return root / "arbiter.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recent_speakers (
            group_id TEXT PRIMARY KEY,
            last_speaker TEXT NOT NULL DEFAULT '',
            last_speak_at TEXT NOT NULL DEFAULT '',
            consecutive_bot_turns INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            group_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            speaker_qq TEXT NOT NULL DEFAULT '',
            speaker_person_key TEXT NOT NULL DEFAULT '',
            speaker_name TEXT NOT NULL DEFAULT '',
            speaker_is_bot INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL DEFAULT '',
            ts TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (group_id, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gm_group_obs ON group_messages(group_id, observed_at)"
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(group_messages)").fetchall()}
    if "speaker_person_key" not in cols:
        conn.execute(
            "ALTER TABLE group_messages ADD COLUMN speaker_person_key TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_bots (
            group_id TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            qq TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            persona_brief TEXT NOT NULL DEFAULT '',
            min_bot_gap_seconds REAL NOT NULL DEFAULT 10,
            max_consecutive_bot_turns INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (group_id, bot_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            speaker TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            since_message_id TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gd_group ON group_decisions(group_id, decision_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_arbitration_silence (
            group_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Helpers shared between both protocols
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _recent_state(conn: sqlite3.Connection, group_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT last_speaker, last_speak_at, consecutive_bot_turns FROM recent_speakers WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    return dict(row) if row else {"last_speaker": "", "last_speak_at": "", "consecutive_bot_turns": 0}


def _cooldown_blocks(state: dict[str, Any], min_gap_seconds: float) -> bool:
    raw = str(state.get("last_speak_at") or "").strip()
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except Exception:
        return False
    return (_now() - last).total_seconds() < min_gap_seconds


def _bump_recent_speaker(conn: sqlite3.Connection, group_id: str, speaker: str) -> None:
    if speaker == "none" or not speaker:
        return
    state = _recent_state(conn, group_id)
    consecutive = int(state.get("consecutive_bot_turns") or 0)
    if str(state.get("last_speaker") or "") == speaker:
        consecutive += 1
    else:
        consecutive = 1
    conn.execute(
        """
        INSERT INTO recent_speakers (group_id, last_speaker, last_speak_at, consecutive_bot_turns)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            last_speaker = excluded.last_speaker,
            last_speak_at = excluded.last_speak_at,
            consecutive_bot_turns = excluded.consecutive_bot_turns
        """,
        (group_id, speaker, _iso(_now()), consecutive),
    )


def _group_silence_enabled(conn: sqlite3.Connection, group_id: str) -> bool:
    row = conn.execute(
        "SELECT enabled FROM group_arbitration_silence WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    return bool(row and int(row["enabled"] or 0))


def is_group_arbitration_silenced(group_id: str) -> bool:
    """Return whether ``group_id`` is in forced ``speaker=none`` mode (see ``/silence``)."""
    group_id = str(group_id or "").strip()
    if not group_id:
        return False
    conn = _connect()
    try:
        return _group_silence_enabled(conn, group_id)
    finally:
        conn.close()


def set_group_arbitration_silence(group_id: str, enabled: bool) -> dict[str, Any]:
    """Persist per-group arbitration silence."""
    group_id = str(group_id or "").strip()
    if not group_id:
        return {"ok": False, "error": "missing_group_id"}
    now_iso = _iso(_now())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO group_arbitration_silence (group_id, enabled, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (group_id, 1 if enabled else 0, now_iso),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "group_id": group_id, "enabled": bool(enabled), "updated_at": now_iso}


def _parse_llm_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    start = text.find("{")
    if start >= 0:
        text = text[start:]
    try:
        value = json.loads(text)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _llm_decide(
    context_text: str,
    candidates: dict[str, dict[str, str]],
    state: dict[str, Any],
) -> tuple[str, str, float]:
    """Call the judge LLM and return ``(speaker, reason, confidence)``.

    ``reason`` carries an explicit fallback tag whenever the LLM short-circuits
    so the audit log is self-describing instead of a generic ``"llm"``.
    """
    if not candidates:
        return "none", "no_candidates", 0.0
    try:
        from pupu.llm import JUDGE_MODEL, json_task

        candidate_lines = []
        for bot_id, candidate in sorted(candidates.items()):
            candidate_lines.append(
                f"- {bot_id}: name={candidate.get('name') or bot_id}, "
                f"qq={candidate.get('qq') or 'unknown'}, persona={candidate.get('persona_brief') or '未提供'}"
            )
        system = (
            "你是群聊发言仲裁器。从候选 bot 里选出本轮最适合接话的一位。\n"
            "【输出格式】只输出一个 JSON 对象，不要 markdown 代码块、不要前言后语、不要注释。"
            "对象必须且只能包含三个键：speaker（字符串）、reason（字符串）、confidence（数字，0 到 1 之间的小数，不要用字符串形式）。\n"
            "【语法】必须是可被标准 JSON 解析器解析的文本：键与字符串用英文双引号；"
            "confidence 写完数字后紧跟 } 或前面的逗号，禁止在数字后再多写一个引号（错误示例：\"confidence\":0.9\" ）；"
            "不要在最后一个字段后面多加逗号。\n"
            "speaker 取值为候选列表中的某位 bot_id，或仅在全场都不适合任何 bot 开口时填 none。"
            "用户之间日常聊天、闲聊、吐槽时也可以自然接梗、关心或打趣；不要因为「只是用户在对话」就频繁选 none。"
        )
        user_content = (
            f"候选 bot:\n{chr(10).join(candidate_lines)}\n\n"
            f"上一轮发言: {state.get('last_speaker') or 'none'}\n"
            f"最近群聊上下文:\n{context_text[-6000:]}\n\n"
            "请选择本轮 speaker。"
        )
        model = (os.environ.get("PUPU_ARBITER_JUDGE_MODEL") or "").strip() or JUDGE_MODEL
        raw = json_task(
            role="judge",
            model=model,
            system=system,
            user_content=user_content,
            max_tokens=10000,
            task_name="group_arbitration",
        )
    except Exception as exc:
        # Distinguish broad failure shapes so the audit log makes triage easy.
        type_name = type(exc).__name__.lower()
        if "timeout" in type_name:
            return "none", "llm_timeout", 0.0
        if "http" in type_name or "connection" in type_name:
            return "none", f"llm_http_error:{type(exc).__name__}", 0.0
        return "none", f"llm_failed:{type(exc).__name__}", 0.0

    raw_text = str(raw or "")
    if not raw_text.strip():
        return "none", "llm_empty_response", 0.0
    data = _parse_llm_json(raw_text)
    if not data:
        _append_judge_error_log(event="judge llm_invalid_json", body=raw_text)
        return "none", "llm_invalid_json", 0.0
    speaker = str(data.get("speaker") or "").strip() or "none"
    reason = str(data.get("reason") or "").strip() or "llm_no_reason"
    try:
        confidence = float(data.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if speaker != "none" and speaker not in candidates:
        return "none", f"llm_invalid_speaker:{speaker[:32]}", 0.0
    if speaker != "none" and len(candidates) < 2:
        # The LLM only had one bot to pick from; stamp the reason so audit
        # readers know the choice was effectively forced.
        reason = f"single_candidate:{reason}"
    return speaker, reason, confidence


def _avoid_low_confidence_repeat(
    speaker: str,
    confidence: float,
    candidates: dict[str, dict[str, str]],
    state: dict[str, Any],
) -> str:
    last = str(state.get("last_speaker") or "")
    if speaker != last or confidence >= 0.75:
        return speaker
    for bot_id in sorted(candidates):
        if bot_id != last:
            return bot_id
    return speaker


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _audit_log_file() -> Path:
    return instances_dir() / "_shared" / "arbiter_audit.log"


def _judge_error_log_file() -> Path:
    """Human-readable judge failures (e.g. JSON parse); UTF-8 filename under ``_shared``."""
    return instances_dir() / "_shared" / "错误.log"


def _append_judge_error_log(*, event: str, body: str) -> None:
    """Append one timestamped block to ``错误.log`` (thread-safe)."""
    path = _judge_error_log_file()
    ts = datetime.now().isoformat(timespec="seconds")
    text = str(body or "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _JUDGE_ERROR_LOG_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"===== {ts} {event} =====\n")
                handle.write(text)
                if text and not text.endswith("\n"):
                    handle.write("\n")
                handle.write("\n")
    except Exception as exc:
        print(f"[arbiter] judge error log write failed: {exc}", file=sys.stderr, flush=True)


def _audit_enabled() -> bool:
    return str(os.environ.get("PUPU_ARBITER_AUDIT", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _append_audit_record(
    *,
    group_id: str,
    round_id: str,
    speaker: str,
    reason: str,
    confidence: float,
    requesting_bot: str,
    candidates: str,
    source: str,
    context_preview: str,
) -> None:
    if not _audit_enabled():
        return
    log_path = _audit_log_file()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        row: dict[str, Any] = {
            "ts": ts,
            "group_id": group_id,
            "round_id": round_id,
            "requesting_bot": requesting_bot,
            "speaker": speaker,
            "reason": reason,
            "confidence": round(float(confidence), 4) if confidence is not None else 0.0,
            "candidates": candidates,
            "source": source,
            "context_preview": context_preview[:4000],
        }
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[arbiter] audit log write failed: {exc}")


def _shorten_preview(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = "…" + text[-limit:]
    return text.replace("\r", " ").replace("\n", "↵")


def _explicit_at_speaker(targets: set[str], candidates: dict[str, dict[str, str]]) -> str | None:
    for bot_id, candidate in candidates.items():
        if candidate.get("qq") and candidate["qq"] in targets:
            return bot_id
    return None


# ---------------------------------------------------------------------------
# Current centralized-debounce protocol
# ---------------------------------------------------------------------------


_AT_PATTERN = re.compile(r"@(\d{5,})")


def _latest_decision_id(conn: sqlite3.Connection, group_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(decision_id) AS max_id FROM group_decisions WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    return int(row["max_id"] or 0) if row else 0


def _decision_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "decision_id": int(data.get("decision_id") or 0),
        "group_id": str(data.get("group_id") or ""),
        "speaker": str(data.get("speaker") or "none"),
        "reason": str(data.get("reason") or ""),
        "confidence": float(data.get("confidence") or 0.0),
        "since_message_id": str(data.get("since_message_id") or ""),
        "decided_at": str(data.get("decided_at") or ""),
        "expires_at": str(data.get("expires_at") or ""),
    }


def load_decision_after(group_id: str, since: int) -> dict[str, Any] | None:
    """Return the first decision with ``decision_id > since`` for this group."""
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT decision_id, group_id, speaker, reason, confidence,
                   since_message_id, decided_at, expires_at
            FROM group_decisions
            WHERE group_id = ? AND decision_id > ?
            ORDER BY decision_id ASC
            LIMIT 1
            """,
            (group_id, int(since)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _decision_row_to_dict(row)


def observe(payload: dict[str, Any]) -> dict[str, Any]:
    """Record a single observed group message and the reporter bot's identity.

    Idempotent on ``(group_id, message_id)``. Returns the latest known
    ``decision_id`` for the group so the caller can use it as a long-poll
    cursor on first call.
    """
    group_id = str(payload.get("group_id") or "").strip()
    message_id = str(payload.get("message_id") or "").strip()
    if not group_id or not message_id:
        return {"ok": False, "error": "missing_group_or_message_id"}

    text = str(payload.get("text") or "")
    speaker_qq = str(payload.get("speaker_qq") or "").strip()
    speaker_person_key = str(payload.get("speaker_person_key") or "").strip()
    speaker_name = str(payload.get("speaker_name") or "").strip()
    speaker_is_bot = 1 if bool(payload.get("speaker_is_bot")) else 0
    ts = str(payload.get("ts") or "").strip() or _iso(_now())

    reporter = payload.get("reporter") or {}
    if not isinstance(reporter, dict):
        reporter = {}
    reporter_bot_id = str(reporter.get("bot_id") or "").strip()

    now_iso = _iso(_now())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO group_messages
                (group_id, message_id, speaker_qq, speaker_person_key, speaker_name, speaker_is_bot, text, ts, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                message_id,
                speaker_qq,
                speaker_person_key,
                speaker_name,
                speaker_is_bot,
                text,
                ts,
                now_iso,
            ),
        )
        if reporter_bot_id:
            reporter_qq = str(reporter.get("qq") or "").strip()
            if reporter_qq:
                conn.execute(
                    """
                    DELETE FROM group_bots
                    WHERE group_id = ? AND qq = ? AND bot_id != ?
                    """,
                    (group_id, reporter_qq, reporter_bot_id),
                )
            min_gap = reporter.get("min_bot_gap_seconds")
            try:
                min_gap_val = float(min_gap) if min_gap is not None else 10.0
            except (TypeError, ValueError):
                min_gap_val = 10.0
            max_cons = reporter.get("max_consecutive_bot_turns")
            try:
                max_cons_val = int(max_cons) if max_cons is not None else 0
            except (TypeError, ValueError):
                max_cons_val = 0
            conn.execute(
                """
                INSERT INTO group_bots
                    (group_id, bot_id, qq, name, persona_brief,
                     min_bot_gap_seconds, max_consecutive_bot_turns, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, bot_id) DO UPDATE SET
                    qq = excluded.qq,
                    name = excluded.name,
                    persona_brief = excluded.persona_brief,
                    min_bot_gap_seconds = excluded.min_bot_gap_seconds,
                    max_consecutive_bot_turns = excluded.max_consecutive_bot_turns,
                    last_seen = excluded.last_seen
                """,
                (
                    group_id,
                    reporter_bot_id,
                    reporter_qq,
                    str(reporter.get("name") or reporter_bot_id).strip(),
                    str(reporter.get("persona_brief") or "").strip(),
                    max(0.0, min(600.0, min_gap_val)),
                    max(0, min(99, max_cons_val)),
                    now_iso,
                ),
            )
        # Optional: peers list piggy-backed by the reporter (best-effort registration).
        for peer in payload.get("peers") or []:
            if not isinstance(peer, dict):
                continue
            peer_bot = str(peer.get("bot_id") or "").strip()
            if not peer_bot:
                continue
            conn.execute(
                """
                INSERT INTO group_bots
                    (group_id, bot_id, qq, name, persona_brief,
                     min_bot_gap_seconds, max_consecutive_bot_turns, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, bot_id) DO UPDATE SET
                    qq = COALESCE(NULLIF(excluded.qq, ''), group_bots.qq),
                    name = COALESCE(NULLIF(excluded.name, ''), group_bots.name),
                    persona_brief = COALESCE(NULLIF(excluded.persona_brief, ''), group_bots.persona_brief),
                    last_seen = group_bots.last_seen
                """,
                (
                    group_id,
                    peer_bot,
                    str(peer.get("qq") or "").strip(),
                    str(peer.get("name") or peer_bot).strip(),
                    str(peer.get("persona_brief") or "").strip(),
                    10.0,
                    0,
                    now_iso,
                ),
            )
        conn.commit()
        latest = _latest_decision_id(conn, group_id)
    finally:
        conn.close()
    return {"ok": True, "group_id": group_id, "message_id": message_id, "latest_decision_id": latest}


def _load_recent_messages(conn: sqlite3.Connection, group_id: str, limit: int = 80) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT message_id, speaker_qq, speaker_person_key, speaker_name, speaker_is_bot, text, ts
        FROM group_messages
        WHERE group_id = ?
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        (group_id, int(limit)),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _load_group_bots(conn: sqlite3.Connection, group_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT bot_id, qq, name, persona_brief, min_bot_gap_seconds, max_consecutive_bot_turns
        FROM group_bots
        WHERE group_id = ?
        """,
        (group_id,),
    ).fetchall()
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        bot_id = str(row["bot_id"] or "").strip()
        if not bot_id:
            continue
        candidates[bot_id] = {
            "bot_id": bot_id,
            "qq": str(row["qq"] or "").strip(),
            "name": str(row["name"] or bot_id).strip(),
            "persona_brief": str(row["persona_brief"] or "").strip(),
            "min_bot_gap_seconds": float(row["min_bot_gap_seconds"] or 10.0),
            "max_consecutive_bot_turns": int(row["max_consecutive_bot_turns"] or 0),
        }
    return candidates


def _build_recent_context(messages: list[dict[str, Any]]) -> tuple[str, set[str], str]:
    """Return ``(context_text, at_targets, since_message_id)``."""
    lines: list[str] = []
    at_targets: set[str] = set()
    since_msg = ""
    for msg in messages:
        text = str(msg.get("text") or "").strip()
        speaker_name = str(msg.get("speaker_name") or "").strip()
        speaker_qq = str(msg.get("speaker_qq") or "").strip()
        speaker_is_bot = bool(msg.get("speaker_is_bot"))
        display = speaker_name or speaker_qq or "user"
        if speaker_is_bot:
            label = f"[bot {display}]"
        else:
            label = f"[{display}]"
        line = f"{label} {text}".strip()
        if line:
            lines.append(line)
        at_targets.update(_AT_PATTERN.findall(text))
        if msg.get("message_id"):
            since_msg = str(msg["message_id"])
    return "\n".join(lines), at_targets, since_msg


def run_judge(group_id: str, *, source: str = "scheduled") -> dict[str, Any] | None:
    """Run the judge for ``group_id`` exactly once and persist the decision.

    Called from the embedded debounce runtime (via ``asyncio.to_thread``).
    Returns the new decision dict, or ``None`` if there was nothing to judge
    (no messages observed yet, or no candidate bots registered).
    """
    group_id = str(group_id or "").strip()
    if not group_id:
        return None

    with _group_lock(group_id):
        conn = _connect()
        try:
            messages = _load_recent_messages(conn, group_id)
            if not messages:
                return None
            candidates = _load_group_bots(conn, group_id)
            if not candidates:
                return None

            context_text, at_targets, since_message_id = _build_recent_context(messages)
            preview = _shorten_preview(context_text)
            cand_str = ",".join(sorted(candidates.keys()))
            base_audit = {
                "requesting_bot": "",
                "candidates": cand_str,
                "context_preview": preview,
            }

            state = _recent_state(conn, group_id)
            min_gap = max((c.get("min_bot_gap_seconds") or 10.0) for c in candidates.values())
            max_consecutive = max((c.get("max_consecutive_bot_turns") or 0) for c in candidates.values())

            decision_speaker: str
            decision_reason: str
            decision_confidence: float
            audit_source: str

            explicit = _explicit_at_speaker(at_targets, candidates)
            if _group_silence_enabled(conn, group_id):
                decision_speaker = "none"
                decision_reason = "silence_command"
                decision_confidence = 1.0
                audit_source = "silence_command"
            elif explicit:
                decision_speaker = explicit
                decision_reason = "explicit_at"
                decision_confidence = 1.0
                audit_source = "explicit_at"
            elif _cooldown_blocks(state, float(min_gap)):
                decision_speaker = "none"
                decision_reason = "min_gap"
                decision_confidence = 1.0
                audit_source = "min_gap"
            elif max_consecutive > 0 and int(state.get("consecutive_bot_turns") or 0) >= max_consecutive:
                decision_speaker = "none"
                decision_reason = "max_consecutive"
                decision_confidence = 1.0
                audit_source = "max_consecutive"
            else:
                speaker, reason, confidence = _llm_decide(context_text, candidates, state)
                if speaker != "none":
                    adjusted = _avoid_low_confidence_repeat(speaker, confidence, candidates, state)
                    if adjusted != speaker:
                        speaker = adjusted
                        reason = "avoid_low_confidence_repeat"
                        confidence = max(confidence, 0.55)
                decision_speaker = speaker
                decision_reason = reason
                decision_confidence = confidence
                audit_source = "llm"

            now = _now()
            expires = now + timedelta(seconds=_DECISION_TTL_SECONDS)
            cursor = conn.execute(
                """
                INSERT INTO group_decisions
                    (group_id, speaker, reason, confidence, since_message_id, decided_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    decision_speaker,
                    decision_reason,
                    float(decision_confidence),
                    since_message_id,
                    _iso(now),
                    _iso(expires),
                ),
            )
            decision_id = int(cursor.lastrowid or 0)
            _bump_recent_speaker(conn, group_id, decision_speaker)
            conn.commit()
        finally:
            conn.close()

    _append_audit_record(
        group_id=group_id,
        round_id=f"auto:{decision_id}",
        speaker=decision_speaker,
        reason=decision_reason,
        confidence=float(decision_confidence),
        requesting_bot=base_audit["requesting_bot"],
        candidates=base_audit["candidates"],
        source=audit_source if source == "scheduled" else f"{audit_source}:{source}",
        context_preview=base_audit["context_preview"],
    )

    _signal_group_event(group_id)

    return {
        "decision_id": decision_id,
        "group_id": group_id,
        "speaker": decision_speaker,
        "reason": decision_reason,
        "confidence": float(decision_confidence),
        "since_message_id": since_message_id,
        "decided_at": _iso(now),
        "expires_at": _iso(expires),
    }


async def await_decision_async(group_id: str, since: int, timeout: float) -> dict[str, Any] | None:
    """Long-poll for the next decision with ``decision_id > since``.

    Returns the decision dict, or ``None`` on timeout. Safe to call from the
    embedded arbiter runtime's event loop.
    """
    group_id = str(group_id or "").strip()
    if not group_id:
        return None

    timeout = max(0.0, min(120.0, float(timeout)))
    deadline = asyncio.get_running_loop().time() + timeout

    # Fast path: already have a fresher decision.
    existing = load_decision_after(group_id, since)
    if existing:
        return existing

    while True:
        event = _ensure_group_event(group_id)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        existing = load_decision_after(group_id, since)
        if existing:
            return existing
        # Spurious wakeup or a decision that's older than ``since``: keep waiting.

    return load_decision_after(group_id, since)
