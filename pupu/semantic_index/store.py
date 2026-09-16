"""SQLite store for semantic index cards."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..storage.db import get_conn
from .vector import cosine_similarity, pack_vector, unpack_vector


@dataclass(frozen=True, slots=True)
class SemanticCard:
    id: int
    source_type: str
    source_key: str
    source_id: str
    source_version: str
    projection_kind: str
    text: str
    embedding: list[float]
    embedding_model: str
    embedding_dim: int
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    score: float | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _metadata_json(metadata: dict[str, Any] | None) -> str:
    return json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)


def _row_to_card(row: Any, *, score: float | None = None) -> SemanticCard:
    metadata = {}
    try:
        parsed = json.loads(row["metadata_json"] or "{}")
        if isinstance(parsed, dict):
            metadata = parsed
    except Exception:
        metadata = {}
    return SemanticCard(
        id=int(row["id"]),
        source_type=row["source_type"],
        source_key=row["source_key"],
        source_id=row["source_id"],
        source_version=row["source_version"],
        projection_kind=row["projection_kind"],
        text=row["text"],
        embedding=unpack_vector(row["embedding"]),
        embedding_model=row["embedding_model"],
        embedding_dim=int(row["embedding_dim"] or 0),
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        score=score,
    )


def upsert_card(
    *,
    source_type: str,
    source_key: str,
    source_id: str | int | None,
    source_version: str,
    text: str,
    embedding: list[float],
    embedding_model: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    now = _now()
    packed = pack_vector(embedding)
    conn = get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO semantic_cards (
                   source_type, source_key, source_id, source_version,
                   projection_kind, text, embedding, embedding_model,
                   embedding_dim, metadata_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'rag_card', ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_type, source_key)
               DO UPDATE SET
                   source_id = excluded.source_id,
                   source_version = excluded.source_version,
                   projection_kind = excluded.projection_kind,
                   text = excluded.text,
                   embedding = excluded.embedding,
                   embedding_model = excluded.embedding_model,
                   embedding_dim = excluded.embedding_dim,
                   metadata_json = excluded.metadata_json,
                   updated_at = excluded.updated_at""",
            (
                str(source_type),
                str(source_key),
                "" if source_id is None else str(source_id),
                str(source_version),
                str(text),
                packed,
                str(embedding_model),
                len(embedding),
                _metadata_json(metadata),
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM semantic_cards WHERE source_type = ? AND source_key = ?",
            (str(source_type), str(source_key)),
        ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)
    finally:
        conn.close()


def list_cards(limit: int | None = None) -> list[SemanticCard]:
    sql = """SELECT * FROM semantic_cards ORDER BY updated_at DESC, id DESC"""
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    conn = get_conn()
    try:
        return [_row_to_card(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def list_source_cards() -> list[SemanticCard]:
    return list_cards(limit=None)


def get_card_by_source(source_type: str, source_key: str) -> SemanticCard | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM semantic_cards WHERE source_type = ? AND source_key = ?",
            (source_type, source_key),
        ).fetchone()
        return _row_to_card(row) if row else None
    finally:
        conn.close()


def delete_cards_by_source_keys(source_keys: list[str] | set[str]) -> int:
    keys = [str(key) for key in source_keys if str(key)]
    if not keys:
        return 0
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in keys)
        cursor = conn.execute(
            f"DELETE FROM semantic_cards WHERE source_key IN ({placeholders})",
            tuple(keys),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def clear_cards() -> int:
    conn = get_conn()
    try:
        cursor = conn.execute("DELETE FROM semantic_cards")
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def search_cards(query_embedding: list[float], *, limit: int) -> list[SemanticCard]:
    cards = [card for card in list_cards(limit=None) if card.embedding]
    scored: list[SemanticCard] = []
    for card in cards:
        if card.embedding_dim and card.embedding_dim != len(query_embedding):
            continue
        score = cosine_similarity(query_embedding, card.embedding)
        scored.append(
            SemanticCard(
                id=card.id,
                source_type=card.source_type,
                source_key=card.source_key,
                source_id=card.source_id,
                source_version=card.source_version,
                projection_kind=card.projection_kind,
                text=card.text,
                embedding=card.embedding,
                embedding_model=card.embedding_model,
                embedding_dim=card.embedding_dim,
                metadata=card.metadata,
                created_at=card.created_at,
                updated_at=card.updated_at,
                score=score,
            )
        )
    scored.sort(key=lambda item: (float(item.score or 0.0), item.updated_at), reverse=True)
    return scored[: max(1, int(limit))]


__all__ = [
    "SemanticCard",
    "clear_cards",
    "delete_cards_by_source_keys",
    "get_card_by_source",
    "list_cards",
    "list_source_cards",
    "search_cards",
    "upsert_card",
]
