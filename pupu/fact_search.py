"""Search helpers for person facts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .memory_index import recall_memories
from .storage.facts import get_person_facts
from .storage.people import normalize_person_key, person_from_session


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _tokens(text: object) -> set[str]:
    raw = str(text or "").lower()
    tokens = set(re.findall(r"[0-9a-zA-Z_\-\u4e00-\u9fff]{2,}", raw))
    for chunk in re.findall(r"[\u4e00-\u9fff]{3,}", raw):
        for size in (2, 3, 4):
            if len(chunk) >= size:
                tokens.update(chunk[index : index + size] for index in range(len(chunk) - size + 1))
    return {token for token in tokens if token.strip()}


def _source_key_for_fact(fact: dict[str, Any]) -> str:
    return ":".join(
        (
            "person_fact",
            quote(str(fact.get("subject_person_key") or ""), safe=""),
            quote(str(fact.get("object_person_key") or ""), safe=""),
            quote(str(fact.get("scope") or "person"), safe=""),
            quote(str(fact.get("fact_key") or ""), safe=""),
        )
    )


def _fact_text(fact: dict[str, Any]) -> str:
    subject = _text(fact.get("subject_display_name") or fact.get("subject_person_key"))
    obj = _text(fact.get("object_display_name") or fact.get("object_person_key"))
    scope = _text(fact.get("scope") or "person")
    key = _text(fact.get("fact_key"))
    value = _text(fact.get("fact_value"))
    label = subject or "相关人物"
    if scope == "relationship" and obj:
        label = f"{label} -> {obj}"
    return f"{label} | {key}: {value}".strip()


def _fact_in_people_scope(fact: dict[str, Any], people: set[str]) -> bool:
    if not people:
        return True
    subject = normalize_person_key(fact.get("subject_person_key"))
    obj = normalize_person_key(fact.get("object_person_key"))
    if not obj:
        return subject in people
    return subject in people and obj in people


def _load_scoped_facts(person_keys: set[str]) -> list[dict[str, Any]]:
    if person_keys:
        return get_person_facts(subject_person_keys=sorted(person_keys), include_relationships=True)
    return get_person_facts(include_relationships=True)


def _score_fact(
    fact: dict[str, Any],
    query_tokens: set[str],
    *,
    memu_score: float | None = None,
    person_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    text = _fact_text(fact)
    hay_tokens = _tokens(text)
    overlap_tokens = sorted(query_tokens & hay_tokens)
    if not overlap_tokens and memu_score is None:
        return None
    overlap_score = len(overlap_tokens) / max(1, min(len(query_tokens), len(hay_tokens)))
    semantic_score = max(0.0, min(1.0, float(memu_score))) if memu_score is not None else 0.0
    scoped_people = {normalize_person_key(item) for item in (person_keys or set()) if normalize_person_key(item)}
    subject = normalize_person_key(fact.get("subject_person_key"))
    obj = normalize_person_key(fact.get("object_person_key"))
    fact_people = {item for item in (subject, obj) if item}
    matched_people = sorted(scoped_people & fact_people)
    people_bonus = 0.16 if scoped_people and matched_people else 0.0
    confidence_bonus = min(0.08, max(0.0, float(fact.get("confidence") or 0.0)) * 0.08)
    recent_bonus = 0.0
    try:
        updated = datetime.fromisoformat(str(fact.get("updated_at") or ""))
        age_days = max(0.0, (datetime.now() - updated).total_seconds() / 86400)
        recent_bonus = max(0.0, 0.12 - min(age_days, 30) * 0.004)
    except Exception:
        pass
    score = max(
        0.0,
        min(
            1.0,
            semantic_score * 0.48
            + overlap_score * 0.36
            + people_bonus
            + confidence_bonus
            + recent_bonus,
        ),
    )
    reason_bits = []
    if memu_score is not None:
        reason_bits.append(f"memu={semantic_score:.2f}")
    if overlap_tokens:
        reason_bits.append("matched: " + ", ".join(overlap_tokens[:8]))
    if people_bonus:
        reason_bits.append(f"people+{people_bonus:.2f}")
    if recent_bonus:
        reason_bits.append(f"recent+{recent_bonus:.2f}")
    if confidence_bonus:
        reason_bits.append(f"confidence+{confidence_bonus:.2f}")
    out = dict(fact)
    out["fact_id"] = int(out.get("id") or 0)
    out["text"] = text
    out["score"] = score
    out["reason_for_match"] = "; ".join(reason_bits) or "local candidate"
    out["match_debug"] = {
        "memu_score": semantic_score,
        "overlap_score": overlap_score,
        "overlap_tokens": overlap_tokens[:12],
        "people_bonus": people_bonus,
        "recent_bonus": recent_bonus,
        "confidence_bonus": confidence_bonus,
        "query_people": sorted(scoped_people),
        "fact_people": sorted(fact_people),
        "matched_people": matched_people,
        "total": score,
        "used_memu_candidate": memu_score is not None,
    }
    return out


def find_related_person_facts(
    text: str,
    *,
    identity_session: str = "owner",
    context_session: str | None = None,
    person_keys: set[str] | list[str] | tuple[str, ...] | None = None,
    limit: int = 8,
    debug: bool = False,
) -> list[dict[str, Any]]:
    query = _text(text)
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    scoped_people = {
        normalize_person_key(item) for item in (person_keys or []) if normalize_person_key(item)
    }
    if not scoped_people and identity_session:
        scoped_people.add(person_from_session(identity_session))

    facts = _load_scoped_facts(scoped_people)
    facts = [fact for fact in facts if _fact_in_people_scope(fact, scoped_people)]
    by_id = {int(fact.get("id") or 0): fact for fact in facts if int(fact.get("id") or 0)}
    by_source_key = {_source_key_for_fact(fact): fact for fact in facts}

    memu_scores: dict[int, float] = {}
    memu_attempted = False
    try:
        memories = recall_memories(
            query=query,
            context_session=context_session or identity_session,
            identity_session=identity_session,
            history=[],
            limit=max(12, int(limit) * 3),
        )
        memu_attempted = True
    except Exception:
        memories = []
    for item in memories:
        if str(item.get("source_type") or item.get("kind") or "") != "person_fact":
            continue
        fact = None
        source_id = item.get("source_id")
        try:
            fact = by_id.get(int(source_id))
        except Exception:
            fact = None
        if fact is None:
            fact = by_source_key.get(str(item.get("source_key") or ""))
        if not fact:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except Exception:
            score = 0.0
        fact_id = int(fact.get("id") or 0)
        memu_scores[fact_id] = max(memu_scores.get(fact_id, 0.0), score)

    scored: list[dict[str, Any]] = []
    for fact in facts:
        fact_id = int(fact.get("id") or 0)
        item = _score_fact(
            fact,
            query_tokens,
            memu_score=memu_scores.get(fact_id),
            person_keys=scoped_people,
        )
        if not item:
            continue
        if debug:
            item["match_debug"]["memu_attempted"] = memu_attempted
        scored.append(item)

    scored.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return scored[: max(1, int(limit))]


def format_related_person_facts(
    query: str,
    *,
    identity_session: str = "owner",
    context_session: str | None = None,
    person_keys: set[str] | list[str] | tuple[str, ...] | None = None,
    limit: int = 8,
    debug: bool = False,
) -> str:
    query = _text(query)
    if not query:
        return "用法：/facts search [--debug] <内容>"
    facts = find_related_person_facts(
        query,
        identity_session=identity_session,
        context_session=context_session,
        person_keys=person_keys,
        limit=limit,
        debug=debug,
    )
    if not facts:
        return f"没有找到相关 facts：{query}"
    lines = [f"相关 facts {len(facts)} 条" + ("（debug）" if debug else "")]
    for index, fact in enumerate(facts, start=1):
        score = float(fact.get("score") or 0.0)
        lines.append(f"{index}. [fact_id={fact.get('fact_id')}] {_fact_text(fact)}")
        lines.append(f"   匹配: score={score:.2f} {_text(fact.get('reason_for_match'))}")
        if debug:
            detail = fact.get("match_debug") or {}
            lines.append(
                "   debug: "
                f"total={float(detail.get('total') or score):.3f} "
                f"memu={float(detail.get('memu_score') or 0.0):.3f} "
                f"overlap={float(detail.get('overlap_score') or 0.0):.3f} "
                f"people_bonus={float(detail.get('people_bonus') or 0.0):.3f} "
                f"recent_bonus={float(detail.get('recent_bonus') or 0.0):.3f} "
                f"confidence_bonus={float(detail.get('confidence_bonus') or 0.0):.3f} "
                f"memu_attempted={bool(detail.get('memu_attempted'))} "
                f"used_memu={bool(detail.get('used_memu_candidate'))}"
            )
            tokens = detail.get("overlap_tokens") or []
            if tokens:
                lines.append(f"   debug_tokens: {', '.join(str(token) for token in tokens[:12])}")
    return "\n".join(lines)
