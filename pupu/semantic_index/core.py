"""Core sync and recall operations for PuPu's built-in semantic index."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .config import is_semantic_index_enabled, semantic_top_k
from .embedding_client import embed_text
from .projection import (
    build_review_entries,
    dedupe_entries_by_source_key,
    entries_with_source_metadata,
    expected_source_entries,
    lookup_source_entry,
)
from .store import (
    clear_cards,
    delete_cards_by_source_keys,
    list_cards,
    list_source_cards,
    search_cards,
    upsert_card,
)


def _log(message: str) -> None:
    print(f"[pupu][semantic] {message}")


def _preview(value: object, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


@dataclass(slots=True)
class SemanticWriteResult:
    status: str
    ids: list[str]
    error: str = ""


def _entry_source_key(entry: tuple[str, str, dict[str, Any]]) -> str:
    return str(entry[2].get("source_key") or "").strip()


def _entry_source_version(entry: tuple[str, str, dict[str, Any]]) -> str:
    return str(entry[2].get("source_version") or "").strip()


def _upsert_entry(entry: tuple[str, str, dict[str, Any]]) -> int:
    kind, text, extra = entry
    source_key = str(extra.get("source_key") or "").strip()
    if not source_key:
        raise ValueError("semantic source entry is missing source_key")
    embedding, model = embed_text(text)
    return upsert_card(
        source_type=str(extra.get("source_type") or kind),
        source_key=source_key,
        source_id=extra.get("source_id"),
        source_version=str(extra.get("source_version") or ""),
        text=text,
        embedding=embedding,
        embedding_model=model,
        metadata={**dict(extra or {}), "kind": kind},
    )


def sync_review_memory(
    *,
    context_session: str,
    identity_session: str,
    start_msg_id: int,
    end_msg_id: int,
    summary: str,
    person_facts: list[dict] | None = None,
    event_threads: list[dict] | None = None,
) -> SemanticWriteResult:
    _log(
        "sync start "
        f"context={context_session} identity={identity_session} range={start_msg_id}..{end_msg_id} "
        f"summary_chars={len(summary or '')} person_facts={len(person_facts or [])} "
        f"event_threads={len(event_threads or [])}"
    )
    if not is_semantic_index_enabled():
        _log(f"sync skipped status=disabled context={context_session} identity={identity_session}")
        return SemanticWriteResult(status="disabled", ids=[])
    entries = build_review_entries(
        summary=summary,
        person_facts=person_facts,
        event_threads=event_threads,
    )
    entries = entries_with_source_metadata(
        entries,
        context_session=context_session,
        start_msg_id=start_msg_id,
        end_msg_id=end_msg_id,
        summary=summary,
        person_facts=person_facts,
        event_threads=event_threads,
    )
    entries = dedupe_entries_by_source_key(entries)
    _log(
        "sync entries "
        f"context={context_session} identity={identity_session} total={len(entries)} "
        f"kinds={dict(Counter(kind for kind, _text, _extra in entries))}"
    )
    if not entries:
        return SemanticWriteResult(status="empty", ids=[])
    ids: list[str] = []
    try:
        for index, entry in enumerate(entries, start=1):
            kind, text, extra = entry
            _log(
                "sync card upsert "
                f"index={index}/{len(entries)} kind={kind} chars={len(text)} "
                f"source_key={extra.get('source_key') or '<none>'} text_preview={_preview(text)}"
            )
            card_id = _upsert_entry(entry)
            ids.append(str(card_id))
    except Exception as exc:
        _log(f"sync failed error={type(exc).__name__}: {_preview(exc, 800)}")
        return SemanticWriteResult(status="failed", ids=[], error=str(exc))
    _log(f"sync success context={context_session} identity={identity_session} ids_count={len(ids)}")
    return SemanticWriteResult(status="success", ids=ids)


def recall_memories(
    *,
    query: str,
    context_session: str,
    identity_session: str,
    history: list[dict] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    requested_limit = limit or semantic_top_k()
    _log(
        "recall start "
        f"context={context_session} identity={identity_session} top_k={requested_limit} "
        f"history_messages={len(history or [])} query_chars={len(query or '')} "
        f"query_preview={_preview(query)}"
    )
    if not is_semantic_index_enabled():
        _log(f"recall skipped status=disabled context={context_session} identity={identity_session}")
        return []
    try:
        query_embedding, _model = embed_text(str(query or ""))
        cards = search_cards(query_embedding, limit=requested_limit * 3)
    except Exception as exc:
        _log(f"recall failed error={type(exc).__name__}: {_preview(exc, 800)}")
        return []

    out: list[dict[str, Any]] = []
    for index, card in enumerate(cards, start=1):
        source_entry = lookup_source_entry(card.source_type, card.source_key)
        if not source_entry:
            _log(
                "recall card skipped "
                f"index={index} reason=missing_sqlite_source source_key={card.source_key}"
            )
            continue
        kind, text, extra = source_entry
        memory = {
            "kind": kind,
            "text": text,
            "source": "semantic_index",
            "score": card.score,
            "created_at": card.created_at,
            "source_type": extra.get("source_type") or card.source_type,
            "source_id": extra.get("source_id") or card.source_id,
            "source_key": extra.get("source_key") or card.source_key,
            "source_version": extra.get("source_version") or card.source_version,
            "projection_kind": extra.get("projection_kind") or card.projection_kind,
        }
        out.append({key: value for key, value in memory.items() if value not in (None, "")})
        _log(
            "recall card "
            f"index={index} accepted={len(out)} kind={kind} score={float(card.score or 0.0):.4f} "
            f"source_key={card.source_key} chars={len(text)} text_preview={_preview(text)}"
        )
        if len(out) >= requested_limit:
            break
    _log(f"recall {'success' if out else 'empty'} context={context_session} identity={identity_session} count={len(out)}")
    return out


def _source_card_report_base(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": "ok",
        "checked": 0,
        "present": 0,
        "missing": 0,
        "created": 0,
        "deleted": 0,
        "refreshed": 0,
        "duplicates": 0,
        "orphaned": 0,
        "failed": 0,
        "source_kind_counts": {},
        "semantic_kind_counts": {},
        "missing_keys": [],
        "orphaned_keys": [],
        "duplicate_keys": [],
        "refreshed_keys": [],
        "error": "",
    }


def reconcile_source_cache(
    identity_session: str,
    *,
    context_session: str | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
) -> dict[str, Any]:
    identity_session = str(identity_session or "default")
    mode = "rebuild" if rebuild else ("check" if dry_run else "apply")
    result = _source_card_report_base(mode)
    expected_entries = expected_source_entries(identity_session)
    expected_by_key = {
        _entry_source_key(entry): entry
        for entry in expected_entries
        if _entry_source_key(entry)
    }
    result["checked"] = len(expected_by_key)
    result["source_kind_counts"] = dict(Counter(kind for kind, _text, _extra in expected_entries))
    if not is_semantic_index_enabled():
        result["status"] = "disabled"
        result["error"] = "semantic index disabled"
        return result

    existing = list_source_cards()
    existing_by_key: dict[str, list[Any]] = {}
    for card in existing:
        existing_by_key.setdefault(card.source_key, []).append(card)
    result["semantic_kind_counts"] = dict(Counter(card.source_type for card in existing))

    missing_keys = [key for key in expected_by_key if key not in existing_by_key]
    orphaned_keys = [key for key in existing_by_key if key not in expected_by_key]
    duplicate_keys = [
        key for key, cards in existing_by_key.items() if key in expected_by_key and len(cards) > 1
    ]
    refreshed_keys: list[str] = []
    for key, cards in existing_by_key.items():
        if key not in expected_by_key or not cards:
            continue
        expected_version = _entry_source_version(expected_by_key[key])
        if expected_version and cards[0].source_version != expected_version:
            refreshed_keys.append(key)
    result.update(
        {
            "present": len(expected_by_key) - len(missing_keys),
            "missing": len(missing_keys),
            "orphaned": len(orphaned_keys),
            "duplicates": sum(max(0, len(cards) - 1) for cards in existing_by_key.values()),
            "refreshed": len(refreshed_keys),
            "missing_keys": missing_keys[:50],
            "orphaned_keys": orphaned_keys[:50],
            "duplicate_keys": duplicate_keys[:50],
            "refreshed_keys": refreshed_keys[:50],
        }
    )
    if dry_run:
        if missing_keys or orphaned_keys or duplicate_keys or refreshed_keys:
            result["status"] = "drift"
        return result

    create_keys: list[str] = []
    if rebuild:
        result["deleted"] = clear_cards()
        create_keys = list(expected_by_key)
    else:
        delete_keys = [*orphaned_keys, *refreshed_keys, *duplicate_keys]
        result["deleted"] = delete_cards_by_source_keys(set(delete_keys))
        create_keys = list(dict.fromkeys(missing_keys + refreshed_keys))
    for key in create_keys:
        entry = expected_by_key.get(key)
        if not entry:
            continue
        try:
            _upsert_entry(entry)
            result["created"] += 1
        except Exception as exc:
            result["failed"] += 1
            _log(f"source card create failed source_key={key} error={type(exc).__name__}: {_preview(exc, 500)}")
    if result["failed"]:
        result["status"] = "partial"
    elif missing_keys or orphaned_keys or duplicate_keys or refreshed_keys or rebuild:
        result["status"] = "synced"
    return result


def rebuild_source_cache(identity_session: str, *, context_session: str | None = None) -> dict[str, Any]:
    return reconcile_source_cache(identity_session, context_session=context_session, rebuild=True)


def sync_missing_event_threads(
    identity_session: str,
    events: list[dict[str, Any]],
    *,
    context_session: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    result = reconcile_source_cache(identity_session, context_session=context_session, dry_run=dry_run)
    result.setdefault("synced", int(result.get("created") or 0))
    return result


def _format_cards(kinds: set[str], empty_text: str, *, limit: int = 200) -> str:
    rows = [card for card in list_cards(limit=limit) if card.source_type in kinds]
    if not rows:
        return empty_text
    lines = [f"语义索引长期记忆 {len(rows)} 条"]
    for index, card in enumerate(rows, start=1):
        lines.append(f"{index}. [{card.source_type}] {card.text}")
    return "\n".join(lines)


def format_semantic_facts_report(identity_session: str) -> str | None:
    if not is_semantic_index_enabled():
        return None
    return _format_cards({"person_fact"}, "当前语义索引里没有 facts 记忆。")


def format_semantic_event_threads_report(identity_session: str) -> str | None:
    if not is_semantic_index_enabled():
        return None
    return _format_cards({"event_thread"}, "当前语义索引里没有事件线记忆。")


def format_semantic_recall_report(query: str, identity_session: str, context_session: str | None = None) -> str:
    memories = recall_memories(
        query=query,
        context_session=context_session or identity_session,
        identity_session=identity_session,
        history=[],
        limit=semantic_top_k(),
    )
    if not memories:
        return "没有从语义索引召回到相关记忆。"
    lines = [f"语义索引召回 {len(memories)} 条"]
    for index, item in enumerate(memories, start=1):
        score = item.get("score")
        score_text = f" score={float(score):.3f}" if isinstance(score, (int, float)) else ""
        lines.append(f"{index}. [{item.get('kind')}] {item.get('text')}{score_text}")
    return "\n".join(lines)


def clear_semantic_index(identity_session: str | None = None) -> int:
    return clear_cards()


def clear_semantic_session(identity_session: str | None = None) -> int:
    session_id = str(identity_session or "").strip()
    if not session_id:
        return clear_semantic_index(identity_session)
    quoted = quote(session_id, safe="")
    delete_keys: set[str] = set()
    for card in list_cards(limit=None):
        if card.source_type == "summary" and card.source_key.startswith(f"summary:{quoted}:"):
            delete_keys.add(card.source_key)
        elif card.source_type == "event_thread" and card.source_key.startswith(f"event_thread:{quoted}:"):
            delete_keys.add(card.source_key)
        elif card.source_type == "person_fact" and not lookup_source_entry(card.source_type, card.source_key):
            delete_keys.add(card.source_key)
    return delete_cards_by_source_keys(delete_keys)


__all__ = [
    "SemanticWriteResult",
    "clear_semantic_index",
    "clear_semantic_session",
    "format_semantic_event_threads_report",
    "format_semantic_facts_report",
    "format_semantic_recall_report",
    "is_semantic_index_enabled",
    "rebuild_source_cache",
    "recall_memories",
    "reconcile_source_cache",
    "sync_missing_event_threads",
    "sync_review_memory",
]
