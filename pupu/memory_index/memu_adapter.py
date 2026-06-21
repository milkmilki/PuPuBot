"""memU-backed long-term memory index for PuPu."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from pydantic import BaseModel

from ..instance_context import require_current_instance_context
from ..persona.core import get_pupu_name
from ..shared_runtime import get_shared_memu_runtime
from ..storage.db import get_conn, get_data_dir
from ..storage.event_threads import get_recent_event_threads
from ..storage.facts import get_person_facts

DEFAULT_TOP_K = 6
DEFAULT_LOG_PREVIEW_CHARS = 220
DEFAULT_RECENCY_DECAY_DAYS = 30.0
DEFAULT_SOURCE_SUMMARY_LIMIT = 80
_service_lock = threading.Lock()
_op_lock = threading.Lock()
_disabled_reasons_logged: set[str] = set()
_config_logged: set[tuple[Any, ...]] = set()
_sqlite_backend_patched = False


def _current_character_name() -> str:
    return get_pupu_name().strip() or "仆仆"


def _replace_default_character_name(text: str, character_name: str | None = None) -> str:
    # Persisted memories are factual text. Do not rewrite "仆仆" here: in group
    # chats it may refer to another real instance/person, not the current bot.
    return str(text or "")


def _speaker_label(role: object, character_name: str | None = None) -> str:
    return "用户" if str(role or "") == "user" else str(character_name or _current_character_name())


def _has_speaker_label(text: object) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    first = next((line.strip() for line in value.splitlines() if line.strip()), "")
    first = re.sub(r"^\[时间:[^\]]+\]\s*", "", first)
    return bool(re.match(r"^[^:：\n]{1,32}\s*[:：]\s*\S", first))


def _format_history_for_recall(
    history: list[dict] | None,
    *,
    character_name: str | None = None,
    limit: int = 6,
) -> str:
    name = str(character_name or _current_character_name())
    lines = []
    for item in (history or [])[-limit:]:
        content = _replace_default_character_name(item.get("content") or "", name).strip()
        if not content:
            continue
        if _has_speaker_label(content):
            lines.append(content)
        else:
            lines.append(f"{_speaker_label(item.get('role'), name)}: {content}")
    return "\n".join(lines)


class PuPuMemoryScope(BaseModel):
    user_id: str | None = None
    session_id: str | None = None
    context_session: str | None = None
    identity_session: str | None = None


@dataclass(slots=True)
class MemuWriteResult:
    status: str
    ids: list[str]
    error: str = ""


SOURCE_BACKED_KINDS = {"summary", "person_fact", "event_thread"}
SOURCE_PROJECTION_KIND = "rag_card"
RELATIVE_TIME_TERMS = (
    "今天",
    "今晚",
    "明天",
    "明晚",
    "后天",
    "刚才",
    "刚刚",
    "最近",
    "现在",
    "正在",
    "准备",
    "马上",
    "等下",
)
STABLE_FACT_TERMS = (
    "喜欢",
    "偏好",
    "习惯",
    "长期",
    "固定",
    "身份",
    "职业",
    "专业",
    "生日",
    "称呼",
    "名字",
    "昵称",
    "学校",
    "大学",
    "项目",
    "技术",
    "关系",
    "设定",
    "自称",
    "性格",
)
LOW_INFO_VALUES = {
    "",
    "true",
    "false",
    "none",
    "null",
    "nil",
    "nan",
    "不知道",
    "不清楚",
    "无",
    "没有",
    "暂无",
    "unknown",
}
PROTECTED_EVENT_KINDS = {"birthday", "anniversary"}
LONG_TERM_EVENT_TERMS = (
    "长期",
    "一直",
    "以后",
    "每晚",
    "每天",
    "每周",
    "每月",
    "每年",
    "固定",
    "纪念日",
    "生日",
    "长期约定",
)


def _log(message: str) -> None:
    print(f"[pupu][memu] {message}")


def _env_bool_auto(name: str, default: str = "auto") -> str:
    return os.environ.get(name, default).strip().lower() or default


def _truthy(value: str) -> bool:
    return value in {"1", "true", "yes", "on", "enabled"}


def _falsey(value: str) -> bool:
    return value in {"0", "false", "no", "off", "disabled"}


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _first_env_name(*names: str) -> str:
    for name in names:
        if os.environ.get(name, "").strip():
            return name
    return ""


def _configured_embedding_key() -> str:
    return _first_env(
        "PUPU_MEMU_EMBED_API_KEY",
        "PUPU_MEMU_API_KEY",
        "OPENAI_API_KEY",
    )


def _configured_embedding_key_name() -> str:
    return _first_env_name(
        "PUPU_MEMU_EMBED_API_KEY",
        "PUPU_MEMU_API_KEY",
        "OPENAI_API_KEY",
    )


def _log_disabled_once(reason: str) -> None:
    if reason not in _disabled_reasons_logged:
        _log(reason)
        _disabled_reasons_logged.add(reason)


def _preview(value: object, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    if limit is None:
        try:
            limit = int(os.environ.get("PUPU_MEMU_LOG_PREVIEW_CHARS", DEFAULT_LOG_PREVIEW_CHARS))
        except Exception:
            limit = DEFAULT_LOG_PREVIEW_CHARS
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "..."


def _json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _canonical_memory_payload_for_hash(value: object) -> tuple[str, dict[str, Any]]:
    """Return a stable memU item summary plus volatile PuPu metadata.

    memU reinforcement hashes the item summary. PuPu's raw payload includes
    volatile fields such as source message range and created_at, so keep the
    hashable summary stable and carry provenance in memU's extra field.
    """
    try:
        payload = json.loads(str(value or ""))
    except Exception:
        return "", {}
    if not isinstance(payload, dict):
        return "", {}
    text = " ".join(str(payload.get("text") or "").split())
    if not text:
        return "", {}
    stable_keys = (
        "kind",
        "text",
        "key",
        "thread_key",
        "event_time",
        "confidence",
        "projection_kind",
        "source_type",
        "source_id",
        "source_key",
        "source_version",
    )
    stable = {key: payload.get(key) for key in stable_keys if payload.get(key) not in (None, "")}
    stable["text"] = text
    volatile = {key: val for key, val in payload.items() if key not in stable}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True), volatile


def is_memu_long_term_enabled() -> bool:
    """Whether long-term memory should be served by memU for this process."""
    raw = _env_bool_auto("PUPU_MEMU_ENABLED", "auto")
    if _falsey(raw):
        _log_disabled_once(f"disabled: PUPU_MEMU_ENABLED={raw}")
        return False
    if not _configured_embedding_key():
        key_names = "PUPU_MEMU_EMBED_API_KEY/PUPU_MEMU_API_KEY/OPENAI_API_KEY"
        if _truthy(raw):
            _log_disabled_once(f"disabled: embedding API key is not configured ({key_names})")
        elif raw == "auto":
            _log_disabled_once(f"auto disabled: embedding API key is not configured ({key_names})")
        return False
    return True


def _memu_db_path() -> Path:
    return require_current_instance_context().memu_db_path


def _sqlite_dsn(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + path.resolve().as_posix()


def _ensure_memu_sqlite_columns() -> None:
    """Lightweight migration for memU DBs created by PuPu's older shim."""
    import sqlite3

    path = _memu_db_path()
    if not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memu_memory_items'"
        ).fetchone()
        if not row:
            return
        columns = {item[1] for item in conn.execute("PRAGMA table_info(memu_memory_items)").fetchall()}
        added: list[str] = []
        if "happened_at" not in columns:
            conn.execute("ALTER TABLE memu_memory_items ADD COLUMN happened_at DATETIME")
            added.append("happened_at")
        if "extra" not in columns:
            conn.execute("ALTER TABLE memu_memory_items ADD COLUMN extra JSON")
            added.append("extra")
        if added:
            conn.commit()
            _log(f"sqlite migration added columns table=memu_memory_items columns={','.join(added)}")
    finally:
        conn.close()


def _top_k() -> int:
    try:
        return max(1, int(os.environ.get("PUPU_MEMU_RETRIEVE_TOP_K", DEFAULT_TOP_K)))
    except Exception:
        return DEFAULT_TOP_K


def _source_summary_limit() -> int:
    try:
        value = int(os.environ.get("PUPU_MEMU_SOURCE_SUMMARY_LIMIT", str(DEFAULT_SOURCE_SUMMARY_LIMIT)))
    except Exception:
        return DEFAULT_SOURCE_SUMMARY_LIMIT
    if value < 0:
        return 0
    return value


def _memu_ranking() -> str:
    value = os.environ.get("PUPU_MEMU_RANKING", "salience").strip().lower()
    return value if value in {"similarity", "salience"} else "salience"


def _recency_decay_days() -> float:
    try:
        return max(1.0, float(os.environ.get("PUPU_MEMU_RECENCY_DECAY_DAYS", DEFAULT_RECENCY_DECAY_DAYS)))
    except Exception:
        return DEFAULT_RECENCY_DECAY_DAYS


def _enable_reinforcement() -> bool:
    raw = os.environ.get("PUPU_MEMU_ENABLE_REINFORCEMENT", "true").strip().lower()
    return not _falsey(raw)


def _native_category_summaries() -> bool:
    raw = os.environ.get("PUPU_MEMU_NATIVE_CATEGORY_SUMMARIES", "false").strip().lower()
    return not _falsey(raw)


def _llm_profiles() -> dict[str, dict[str, Any]]:
    base_url = _first_env("PUPU_MEMU_LLM_BASE_URL", "PUPU_MEMU_BASE_URL", default="https://api.openai.com/v1")
    api_key = _first_env("PUPU_MEMU_LLM_API_KEY", "PUPU_MEMU_API_KEY", "OPENAI_API_KEY")
    chat_model = _first_env("PUPU_MEMU_LLM_MODEL", default="gpt-4o-mini")
    client_backend = _first_env("PUPU_MEMU_CLIENT_BACKEND", default="sdk")

    embed_base_url = _first_env("PUPU_MEMU_EMBED_BASE_URL", "PUPU_MEMU_BASE_URL", default=base_url)
    embed_api_key = _first_env("PUPU_MEMU_EMBED_API_KEY", "PUPU_MEMU_API_KEY", "OPENAI_API_KEY")
    embed_model = _first_env("PUPU_MEMU_EMBED_MODEL", default="text-embedding-3-small")
    embed_backend = _first_env("PUPU_MEMU_EMBED_CLIENT_BACKEND", default=client_backend)

    default_profile = {
        "provider": _first_env("PUPU_MEMU_LLM_PROVIDER", default="openai"),
        "base_url": base_url,
        "api_key": api_key or embed_api_key or "missing",
        "chat_model": chat_model,
        "embed_model": embed_model,
        "client_backend": client_backend,
    }
    embedding_profile = {
        "provider": _first_env("PUPU_MEMU_EMBED_PROVIDER", default="openai"),
        "base_url": embed_base_url,
        "api_key": embed_api_key or api_key or "missing",
        "chat_model": chat_model,
        "embed_model": embed_model,
        "client_backend": embed_backend,
    }
    return {"default": default_profile, "embedding": embedding_profile}


def _log_config_once(reason: str) -> None:
    signature = _memu_config_signature()
    if signature in _config_logged:
        return
    profiles = _llm_profiles()
    default = profiles["default"]
    embedding = profiles["embedding"]
    _log(
        "config "
        f"reason={reason} enabled_env={_env_bool_auto('PUPU_MEMU_ENABLED', 'auto')} "
        f"db={_memu_db_path()} method={os.environ.get('PUPU_MEMU_METHOD', 'rag')} top_k={_top_k()} "
        f"source_summary_limit={_source_summary_limit()} "
        f"ranking={_memu_ranking()} recency_decay_days={_recency_decay_days()} "
        f"reinforcement={'yes' if _enable_reinforcement() else 'no'} "
        f"native_category_summaries={'yes' if _native_category_summaries() else 'no'} "
        f"llm_provider={default.get('provider')} llm_base_url={default.get('base_url')} "
        f"llm_model={default.get('chat_model')} llm_key={'yes' if default.get('api_key') != 'missing' else 'no'} "
        f"embed_provider={embedding.get('provider')} embed_base_url={embedding.get('base_url')} "
        f"embed_model={embedding.get('embed_model')} embed_key={'yes' if _configured_embedding_key() else 'no'} "
        f"embed_key_source={_configured_embedding_key_name() or '<none>'}"
    )
    _config_logged.add(signature)


def _patch_memu_sqlite_backend() -> None:
    """Patch memU SQLite issues without editing site-packages.

    memU 1.5's native SQLite SQLModel schema still exposes inherited
    ``embedding: list[float]`` fields as regular columns on this local
    Python/SQLModel stack, which SQLModel cannot map. Older versions also
    used table names beginning with ``sqlite_``, a prefix SQLite reserves
    for internal objects. Keep this compatibility shim narrowly scoped to
    storage so PuPu can keep using upstream memU's service, retrieval,
    salience ranking, reinforcement, and category pipelines unchanged.
    """
    global _sqlite_backend_patched
    if _sqlite_backend_patched:
        return

    import inspect
    import uuid
    from sqlalchemy import JSON, MetaData, String, Text
    from sqlmodel import Column, Field, Index, SQLModel, func, select

    import pendulum
    from memu.database.models import MemoryItem
    from memu.database.sqlite import schema as sqlite_schema
    from memu.database.sqlite import sqlite as sqlite_store
    from memu.database.sqlite.models import TZDateTime
    from memu.database.sqlite.repositories.memory_item_repo import SQLiteMemoryItemRepo

    original_create_item = SQLiteMemoryItemRepo.create_item
    if not getattr(original_create_item, "_pupu_resource_id_compat", False):
        create_item_params = set(inspect.signature(original_create_item).parameters)

        def create_item_with_optional_resource_id(self, **kwargs):
            pupu_payload_extra: dict[str, Any] = {}
            if "resource_id" in create_item_params:
                kwargs.setdefault("resource_id", None)
            if "reinforce" in create_item_params and _enable_reinforcement():
                kwargs.setdefault("reinforce", True)
                canonical_summary, volatile_extra = _canonical_memory_payload_for_hash(
                    kwargs.get("summary")
                )
                if canonical_summary:
                    pupu_payload_extra = volatile_extra
                    kwargs["summary"] = canonical_summary
            item = original_create_item(self, **kwargs)
            if pupu_payload_extra and getattr(item, "id", None) and hasattr(self, "update_item"):
                try:
                    item = self.update_item(
                        item_id=item.id,
                        extra={"pupu_payload_extra": pupu_payload_extra},
                    )
                except Exception as exc:
                    _log(f"reinforcement metadata update skipped item_id={item.id} error={type(exc).__name__}: {exc}")
            return item

        create_item_with_optional_resource_id._pupu_resource_id_compat = True
        SQLiteMemoryItemRepo.create_item = create_item_with_optional_resource_id

    def _row_to_memory_item(repo, row):
        kwargs = {
            "id": row.id,
            "resource_id": row.resource_id,
            "memory_type": row.memory_type,
            "summary": row.summary,
            "embedding": repo._normalize_embedding(row.embedding_json),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            **repo._scope_kwargs_from(row),
        }
        if "happened_at" in getattr(MemoryItem, "model_fields", {}):
            kwargs["happened_at"] = getattr(row, "happened_at", None)
        if "extra" in getattr(MemoryItem, "model_fields", {}):
            kwargs["extra"] = getattr(row, "extra", None) or {}
        return MemoryItem(**kwargs)

    original_get_item = SQLiteMemoryItemRepo.get_item
    if not getattr(original_get_item, "_pupu_extra_compat", False):

        def get_item_with_extra(self, item_id: str):
            if item_id in self.items:
                return self.items[item_id]
            with self._sessions.session() as session:
                stmt = select(self._memory_item_model).where(self._memory_item_model.id == item_id)
                row = session.exec(stmt).first()
            if row is None:
                return None
            item = _row_to_memory_item(self, row)
            self.items[row.id] = item
            return item

        get_item_with_extra._pupu_extra_compat = True
        SQLiteMemoryItemRepo.get_item = get_item_with_extra

    original_list_items = SQLiteMemoryItemRepo.list_items
    if not getattr(original_list_items, "_pupu_extra_compat", False):

        def list_items_with_extra(self, where=None):
            with self._sessions.session() as session:
                stmt = select(self._memory_item_model)
                filters = self._build_filters(self._memory_item_model, where)
                if filters:
                    stmt = stmt.where(*filters)
                rows = session.exec(stmt).all()

            result = {}
            for row in rows:
                item = _row_to_memory_item(self, row)
                result[row.id] = item
                self.items[row.id] = item
            return result

        list_items_with_extra._pupu_extra_compat = True
        SQLiteMemoryItemRepo.list_items = list_items_with_extra

    try:
        from memu.database.sqlite.models import (
            SQLiteCategoryItemModel,
            SQLiteMemoryCategoryModel,
            SQLiteMemoryItemModel,
            SQLiteResourceModel,
            build_sqlite_table_model,
        )
    except Exception:
        native_sqlite_models_available = False
    else:
        # memU 1.5.1's SQLite builder still lets SQLModel see inherited
        # ``embedding: list[float]`` fields on this Python/SQLModel stack.
        # Keep the native table builder opt-in until upstream no longer needs
        # the clean-table shim.
        native_sqlite_models_available = _truthy(os.environ.get("PUPU_MEMU_USE_NATIVE_SQLMODELS", "false"))

    if native_sqlite_models_available:
        safe_cache = {}

        def get_sqlite_sqlalchemy_models_patched(*, scope_model=None):
            scope = scope_model or BaseModel
            cached = safe_cache.get(scope)
            if cached:
                return cached

            metadata_obj = MetaData()
            resource_model = build_sqlite_table_model(
                scope,
                SQLiteResourceModel,
                tablename="memu_resources",
                metadata=metadata_obj,
            )
            memory_category_model = build_sqlite_table_model(
                scope,
                SQLiteMemoryCategoryModel,
                tablename="memu_memory_categories",
                metadata=metadata_obj,
            )
            memory_item_model = build_sqlite_table_model(
                scope,
                SQLiteMemoryItemModel,
                tablename="memu_memory_items",
                metadata=metadata_obj,
            )
            category_item_model = build_sqlite_table_model(
                scope,
                SQLiteCategoryItemModel,
                tablename="memu_category_items",
                metadata=metadata_obj,
            )

            class SQLiteBase(SQLModel):
                __abstract__ = True
                metadata = metadata_obj

            models = sqlite_schema.SQLiteSQLAModels(
                Base=SQLiteBase,
                Resource=resource_model,
                MemoryCategory=memory_category_model,
                MemoryItem=memory_item_model,
                CategoryItem=category_item_model,
            )
            safe_cache[scope] = models
            return models

        sqlite_schema.get_sqlite_sqlalchemy_models = get_sqlite_sqlalchemy_models_patched
        sqlite_store.get_sqlite_sqlalchemy_models = get_sqlite_sqlalchemy_models_patched
        _sqlite_backend_patched = True
        _log("sqlite backend compatibility patch applied: native models, table_prefix=memu_")
        return

    safe_cache = {}

    def base_fields() -> tuple[dict[str, Any], dict[str, Any]]:
        annotations = {
            "id": str,
            "created_at": datetime,
            "updated_at": datetime,
        }
        fields = {
            "id": Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, index=True, sa_type=String),
            "created_at": Field(
                default_factory=lambda: pendulum.now("UTC"),
                sa_type=TZDateTime,
                sa_column_kwargs={"server_default": func.now()},
            ),
            "updated_at": Field(default_factory=lambda: pendulum.now("UTC"), sa_type=TZDateTime),
        }
        return annotations, fields

    def scoped_table_model(
        *,
        name: str,
        tablename: str,
        metadata: MetaData,
        scope: type[BaseModel],
        annotations: dict[str, Any],
        fields: dict[str, Any],
        table_args: tuple[Any, ...] = (),
    ) -> type[SQLModel]:
        base_annotations, base_field_defs = base_fields()
        all_annotations: dict[str, Any] = {}
        all_fields: dict[str, Any] = {}
        for scope_name, scope_field in getattr(scope, "model_fields", {}).items():
            all_annotations[scope_name] = scope_field.annotation or str | None
            all_fields[scope_name] = Field(default=None, sa_column=Column(String, nullable=True))
        all_annotations.update(base_annotations)
        all_annotations.update(annotations)
        all_fields.update(base_field_defs)
        all_fields.update(fields)

        attrs: dict[str, Any] = {
            "__module__": "memu.database.sqlite.models",
            "__tablename__": tablename,
            "__annotations__": all_annotations,
            "metadata": metadata,
        }
        attrs.update(all_fields)
        if table_args:
            attrs["__table_args__"] = table_args
        return type(name, (SQLModel,), attrs, table=True)

    def get_sqlite_sqlalchemy_models_patched(*, scope_model=None):
        scope = scope_model or BaseModel
        cached = safe_cache.get(scope)
        if cached:
            return cached

        metadata_obj = MetaData()
        resource_model = scoped_table_model(
            name=f"{scope.__name__}MemuResourceTable",
            tablename="memu_resources",
            metadata=metadata_obj,
            scope=scope,
            annotations={
                "url": str,
                "modality": str,
                "local_path": str,
                "caption": str | None,
                "embedding_json": str | None,
            },
            fields={
                "url": Field(sa_column=Column(String, nullable=False)),
                "modality": Field(sa_column=Column(String, nullable=False)),
                "local_path": Field(sa_column=Column(String, nullable=False)),
                "caption": Field(default=None, sa_column=Column(Text, nullable=True)),
                "embedding_json": Field(default=None, sa_column=Column(Text, nullable=True)),
            },
        )
        memory_category_model = scoped_table_model(
            name=f"{scope.__name__}MemuMemoryCategoryTable",
            tablename="memu_memory_categories",
            metadata=metadata_obj,
            scope=scope,
            annotations={
                "name": str,
                "description": str,
                "embedding_json": str | None,
                "summary": str | None,
            },
            fields={
                "name": Field(sa_column=Column(String, nullable=False, index=True)),
                "description": Field(sa_column=Column(Text, nullable=False)),
                "embedding_json": Field(default=None, sa_column=Column(Text, nullable=True)),
                "summary": Field(default=None, sa_column=Column(Text, nullable=True)),
            },
        )
        memory_item_model = scoped_table_model(
            name=f"{scope.__name__}MemuMemoryItemTable",
            tablename="memu_memory_items",
            metadata=metadata_obj,
            scope=scope,
            annotations={
                "resource_id": str | None,
                "memory_type": str,
                "summary": str,
                "embedding_json": str | None,
                "happened_at": datetime | None,
                "extra": dict[str, Any],
            },
            fields={
                "resource_id": Field(default=None, sa_column=Column(String, nullable=True)),
                "memory_type": Field(sa_column=Column(String, nullable=False)),
                "summary": Field(sa_column=Column(Text, nullable=False)),
                "embedding_json": Field(default=None, sa_column=Column(Text, nullable=True)),
                "happened_at": Field(default=None, sa_column=Column(TZDateTime, nullable=True)),
                "extra": Field(default_factory=dict, sa_column=Column(JSON, nullable=True)),
            },
        )
        category_item_model = scoped_table_model(
            name=f"{scope.__name__}MemuCategoryItemTable",
            tablename="memu_category_items",
            metadata=metadata_obj,
            scope=scope,
            annotations={
                "item_id": str,
                "category_id": str,
            },
            fields={
                "item_id": Field(sa_column=Column(String, nullable=False)),
                "category_id": Field(sa_column=Column(String, nullable=False)),
            },
            table_args=(Index("idx_memu_category_items_unique", "item_id", "category_id", unique=True),),
        )

        class SQLiteBase(SQLModel):
            __abstract__ = True
            metadata = metadata_obj

        models = sqlite_schema.SQLiteSQLAModels(
            Base=SQLiteBase,
            Resource=resource_model,
            MemoryCategory=memory_category_model,
            MemoryItem=memory_item_model,
            CategoryItem=category_item_model,
        )
        safe_cache[scope] = models
        return models

    sqlite_schema.get_sqlite_sqlalchemy_models = get_sqlite_sqlalchemy_models_patched
    sqlite_store.get_sqlite_sqlalchemy_models = get_sqlite_sqlalchemy_models_patched
    _sqlite_backend_patched = True
    _log("sqlite backend compatibility patch applied: clean tables, table_prefix=memu_")


def _retrieve_item_config(top_k: int) -> dict[str, Any]:
    config: dict[str, Any] = {"enabled": True, "top_k": top_k}
    try:
        from memu.app.settings import RetrieveItemConfig

        fields = set(getattr(RetrieveItemConfig, "model_fields", {}))
    except Exception:
        fields = set()
    if "ranking" in fields:
        config["ranking"] = _memu_ranking()
    if "recency_decay_days" in fields:
        config["recency_decay_days"] = _recency_decay_days()
    return config


def _memorize_config() -> dict[str, Any]:
    try:
        from memu.app.settings import MemorizeConfig

        fields = set(getattr(MemorizeConfig, "model_fields", {}))
    except Exception:
        fields = set()
    config: dict[str, Any] = {}
    if "enable_item_reinforcement" in fields:
        config["enable_item_reinforcement"] = _enable_reinforcement()
    if "enable_item_references" in fields:
        config["enable_item_references"] = _native_category_summaries()
    return config


def _new_service():
    from memu.app import MemoryService

    _patch_memu_sqlite_backend()
    top_k = _top_k()
    resources_dir = Path(get_data_dir()) / "memu_resources"
    service = MemoryService(
        llm_profiles=_llm_profiles(),
        blob_config={
            "provider": "local",
            "resources_dir": str(resources_dir),
        },
        database_config={
            "metadata_store": {
                "provider": "sqlite",
                "dsn": _sqlite_dsn(_memu_db_path()),
            },
            "vector_index": {"provider": "bruteforce"},
        },
        retrieve_config={
            "method": os.environ.get("PUPU_MEMU_METHOD", "rag").strip() or "rag",
            "route_intention": False,
            "sufficiency_check": False,
            "category": {"enabled": False, "top_k": top_k},
            "item": _retrieve_item_config(top_k),
            "resource": {"enabled": False, "top_k": 0},
        },
        memorize_config=_memorize_config(),
        user_config={"model": PuPuMemoryScope},
    )
    _ensure_memu_sqlite_columns()
    # The patch/create pipeline can ask an LLM to update memU category
    # summaries. Keep that native path by default; allow disabling it for a
    # cheaper embedding-only mode.
    removed_steps = []
    if not _native_category_summaries():
        for pipeline in ("patch_create", "patch_update", "patch_delete"):
            try:
                service.remove_step(target_step_id="persist_index", pipeline=pipeline)
                removed_steps.append(pipeline)
            except Exception:
                pass
    _log(
        "service configured "
        f"resources_dir={resources_dir} vector_index=bruteforce "
        f"ranking={_memu_ranking()} recency_decay_days={_recency_decay_days()} "
        f"reinforcement={'yes' if _enable_reinforcement() else 'no'} "
        f"removed_persist_index={','.join(removed_steps) or '<none>'}"
    )
    return service


def _memu_config_signature() -> tuple[Any, ...]:
    profiles = _llm_profiles()
    default = profiles["default"]
    embedding = profiles["embedding"]
    return (
        str(_memu_db_path()),
        os.environ.get("PUPU_MEMU_METHOD", "rag").strip() or "rag",
        _top_k(),
        _source_summary_limit(),
        _memu_ranking(),
        _recency_decay_days(),
        _enable_reinforcement(),
        _native_category_summaries(),
        tuple(sorted(default.items())),
        tuple(sorted(embedding.items())),
    )


def _get_service():
    with _service_lock:
        if not is_memu_long_term_enabled():
            raise RuntimeError("memU long-term memory is disabled")
        if not _configured_embedding_key():
            raise RuntimeError("memU embedding API key is not configured")
        try:
            service = get_shared_memu_runtime().get_service(
                memu_db_path=_memu_db_path(),
                config_signature=_memu_config_signature(),
                enabled=is_memu_long_term_enabled,
                factory=lambda: _init_shared_memu_service(),
            )
            return service
        except Exception as exc:
            _log(f"service unavailable error={type(exc).__name__}: {_preview(exc, 500)}")
            raise


def _init_shared_memu_service():
    _log_config_once("service_init")
    _log("service init start")
    service = _new_service()
    _log(f"service enabled db={_memu_db_path()}")
    return service


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("memU sync helper cannot run inside an active event loop; call it in a thread")


def _scope(identity_session: str, context_session: str | None = None) -> dict[str, str]:
    identity = str(identity_session or context_session or "default")
    context = str(context_session or identity)
    return {
        "user_id": identity,
        "session_id": identity,
        "identity_session": identity,
        "context_session": context,
    }


def _global_cache_where() -> dict[str, Any]:
    # memU runs against one SQLite cache DB per PuPu instance. Keep
    # identity/context in item payloads for provenance, but do not use them as
    # recall/list filters; group and private memories should be mutually
    # available to the same instance.
    return {}


def _memory_payload(kind: str, text: str, **extra: Any) -> str:
    payload = {
        "kind": kind,
        "text": " ".join(str(text or "").split()),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _short_hash(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _source_version(*values: object) -> str:
    return _short_hash([value for value in values])


def _summary_source_key(context_session: str, start_msg_id: int, end_msg_id: int) -> str:
    return f"summary:{quote(str(context_session or ''), safe='')}:{int(start_msg_id or 0)}:{int(end_msg_id or 0)}"


def _person_fact_source_key(fact: dict[str, Any]) -> str:
    subject = str(fact.get("subject_person_key") or fact.get("subject") or "").strip()
    obj = str(fact.get("object_person_key") or fact.get("object") or "").strip()
    scope = str(fact.get("scope") or "person").strip()
    key = str(fact.get("fact_key") or fact.get("key") or "").strip()
    return ":".join(
        (
            "person_fact",
            quote(subject, safe=""),
            quote(obj, safe=""),
            quote(scope, safe=""),
            quote(key, safe=""),
        )
    )


def _event_thread_source_key(event: dict[str, Any]) -> str:
    session_id = str(event.get("session_id") or "").strip()
    thread_key = str(event.get("thread_key") or event.get("key") or "").strip()
    return ":".join(
        (
            "event_thread",
            quote(session_id, safe=""),
            quote(thread_key, safe=""),
        )
    )


def _summary_source_version(summary: dict[str, Any]) -> str:
    return _source_version(
        summary.get("session_id"),
        summary.get("summary"),
        summary.get("start_msg_id"),
        summary.get("end_msg_id"),
    )


def _person_fact_source_version(fact: dict[str, Any]) -> str:
    return _source_version(
        fact.get("id"),
        fact.get("subject_person_key"),
        fact.get("object_person_key"),
        fact.get("scope"),
        fact.get("fact_key"),
        fact.get("fact_value"),
        fact.get("confidence"),
        fact.get("updated_at"),
    )


def _event_thread_source_version(event: dict[str, Any]) -> str:
    return _source_version(
        event.get("id"),
        event.get("session_id"),
        event.get("thread_key") or event.get("key"),
        event.get("title"),
        event.get("status"),
        event.get("event_time"),
        event.get("details"),
        event.get("current_summary"),
        event.get("current_cause"),
        event.get("current_reflection"),
        event.get("followup_hint"),
        event.get("confidence"),
        event.get("last_seen_at") or event.get("updated_at"),
    )


def _payload_with_extra(item: dict[str, Any]) -> dict[str, Any]:
    payload = _parse_memory_payload(item.get("summary") or item.get("content"))
    item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    payload_extra = item_extra.get("pupu_payload_extra") if isinstance(item_extra, dict) else None
    if isinstance(payload_extra, dict):
        payload = {**payload_extra, **payload}
    return payload


def _parse_memory_payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {"kind": "other", "text": ""}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed.setdefault("kind", "other")
            parsed.setdefault("text", text)
            return parsed
    except Exception:
        pass
    return {"kind": "other", "text": text}


def _payload_from_item(item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    raw = item.get("summary") or item.get("content")
    parse_failed = False
    if not isinstance(raw, dict):
        try:
            parsed = json.loads(str(raw or "").strip())
            if not isinstance(parsed, dict):
                parse_failed = True
        except Exception:
            parse_failed = bool(str(raw or "").strip())
    payload = _parse_memory_payload(raw)
    item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    payload_extra = item_extra.get("pupu_payload_extra") if isinstance(item_extra, dict) else None
    if isinstance(payload_extra, dict):
        payload = {**payload_extra, **payload}
    return payload, parse_failed


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text[:10] + "T23:59:59")
    except Exception:
        return None


def _norm_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _fact_key_value(payload: dict[str, Any]) -> tuple[str, str]:
    key = str(payload.get("key") or "").strip()
    text = _norm_text(payload.get("text"))
    value = ""
    for sep in (":", "："):
        if sep in text:
            left, right = text.split(sep, 1)
            key = key or left.strip()
            value = right.strip()
            break
    if not value and key and text.startswith(key):
        value = text[len(key) :].strip(" :：")
    if not value and not key:
        value = text
    return key, value


def _has_relative_time(text: str) -> bool:
    return any(term in text for term in RELATIVE_TIME_TERMS)


def _looks_stable_fact(text: str) -> bool:
    return any(term in text for term in STABLE_FACT_TERMS)


def _is_low_info_fact(payload: dict[str, Any]) -> bool:
    key, value = _fact_key_value(payload)
    text = _norm_text(payload.get("text"))
    value_norm = value.strip().lower()
    if len(text) < 4:
        return True
    if value_norm in LOW_INFO_VALUES:
        return True
    if not key and len(text) < 8:
        return True
    return False


def _source_action(candidate: dict[str, Any]) -> str:
    return "none"


def _delete_source(identity_session: str, candidate: dict[str, Any]) -> int:
    return 0


def _load_event_thread_map(identity_session: str) -> dict[str, dict[str, Any]]:
    events = get_recent_event_threads(identity_session, limit=None)
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        key = str(event.get("thread_key") or "").strip()
        if key:
            out[key] = dict(event)
    return out


def _event_date_label(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _parse_event_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _absolutize_event_text(text: str, event_date: date | None) -> str:
    value = str(text or "").strip()
    if not value or not event_date:
        return value
    label = _event_date_label(event_date)
    replacements = [
        ("今天晚上", f"{label}晚上"),
        ("今晚", f"{label}晚上"),
        ("今夜", f"{label}晚上"),
        ("今天早上", f"{label}早上"),
        ("今早", f"{label}早上"),
        ("今天上午", f"{label}上午"),
        ("今天中午", f"{label}中午"),
        ("今天下午", f"{label}下午"),
        ("今天", label),
        ("今日", label),
        ("明天晚上", f"{label}晚上"),
        ("明晚", f"{label}晚上"),
        ("明天早上", f"{label}早上"),
        ("明早", f"{label}早上"),
        ("明天", label),
        ("后天晚上", f"{label}晚上"),
        ("后天", label),
    ]
    for needle, replacement in replacements:
        value = value.replace(needle, replacement)
    if label not in value and any(word in value for word in ("早上", "上午", "中午", "下午", "晚上", "夜里", "夜晚")):
        if value.startswith(("早上", "上午", "中午", "下午", "晚上", "夜里", "夜晚")):
            return label + value
        return f"{label}，{value}"
    return value


def _categories_for(kind: str) -> list[str]:
    if kind == "person_fact":
        return ["personal_info", "preferences", "relationships"]
    if kind == "event_thread":
        return ["experiences", "goals", "relationships"]
    if kind == "summary":
        return ["experiences"]
    return ["knowledge"]


def _memory_type_for(kind: str) -> str:
    if kind == "event_thread":
        return "event"
    if kind == "person_fact":
        return "profile"
    return "knowledge"


def _extract_item_id(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("id", "item_id", "memory_id"):
        value = result.get(key)
        if value:
            return str(value)
    for item_key in ("item", "memory_item"):
        item = result.get(item_key)
        if isinstance(item, dict):
            for key in ("id", "item_id", "memory_id"):
                value = item.get(key)
                if value:
                    return str(value)
    return ""


def _summary_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": row["session_id"],
        "summary": row["summary"],
        "start_msg_id": int(row["start_msg_id"]),
        "end_msg_id": int(row["end_msg_id"]),
        "created_at": row["created_at"],
    }


def _load_all_summaries_from_sqlite() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        limit = _source_summary_limit()
        if limit > 0:
            rows = conn.execute(
                """SELECT id, session_id, summary, start_msg_id, end_msg_id, created_at
                   FROM summaries
                   ORDER BY created_at DESC, id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return list(reversed([_summary_row_to_dict(row) for row in rows]))
        rows = conn.execute(
            """SELECT id, session_id, summary, start_msg_id, end_msg_id, created_at
               FROM summaries
               ORDER BY created_at ASC, id ASC"""
        ).fetchall()
        return [_summary_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def _load_summary_by_source_key(source_key: str) -> dict[str, Any] | None:
    parts = str(source_key or "").split(":")
    if len(parts) < 4 or parts[0] != "summary":
        return None
    try:
        start_msg_id = int(parts[-2])
        end_msg_id = int(parts[-1])
    except Exception:
        return None
    context_session = unquote(":".join(parts[1:-2]))
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, session_id, summary, start_msg_id, end_msg_id, created_at
               FROM summaries
               WHERE session_id = ? AND start_msg_id = ? AND end_msg_id = ?
               ORDER BY id DESC
               LIMIT 1""",
            (context_session, start_msg_id, end_msg_id),
        ).fetchone()
        return _summary_row_to_dict(row) if row else None
    finally:
        conn.close()


def _load_all_event_threads_from_sqlite() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT session_id FROM event_threads ORDER BY session_id ASC").fetchall()
        session_ids = [str(row["session_id"] or "").strip() for row in rows if str(row["session_id"] or "").strip()]
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    for session_id in session_ids:
        events.extend(get_recent_event_threads(session_id, limit=None))
    return events


def _load_event_thread_by_source_key(source_key: str) -> dict[str, Any] | None:
    parts = str(source_key or "").split(":")
    if len(parts) < 3 or parts[0] != "event_thread":
        return None
    session_id = unquote(parts[1])
    thread_key = unquote(":".join(parts[2:]))
    for event in get_recent_event_threads(session_id, limit=None):
        if str(event.get("thread_key") or "") == thread_key:
            return event
    return None


def _load_person_fact_by_source_key(source_key: str) -> dict[str, Any] | None:
    parts = str(source_key or "").split(":")
    if len(parts) < 5 or parts[0] != "person_fact":
        return None
    subject = unquote(parts[1])
    obj = unquote(parts[2])
    scope = unquote(parts[3])
    key = unquote(":".join(parts[4:]))
    for fact in get_person_facts(include_relationships=True):
        if (
            str(fact.get("subject_person_key") or "") == subject
            and str(fact.get("object_person_key") or "") == obj
            and str(fact.get("scope") or "") == scope
            and str(fact.get("fact_key") or "") == key
        ):
            return fact
    return None


def _source_backed_payload(kind: str, text: str, source: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    source_id = source.get("id")
    source_key = ""
    source_version = ""
    if kind == "summary":
        source_key = _summary_source_key(
            str(source.get("session_id") or ""),
            int(source.get("start_msg_id") or 0),
            int(source.get("end_msg_id") or 0),
        )
        source_version = _summary_source_version(source)
    elif kind == "person_fact":
        source_key = _person_fact_source_key(source)
        source_version = _person_fact_source_version(source)
    elif kind == "event_thread":
        source_key = _event_thread_source_key(source)
        source_version = _event_thread_source_version(source)
    payload = {
        "source_type": kind,
        "source_id": source_id,
        "source_key": source_key,
        "source_version": source_version,
        "projection_kind": SOURCE_PROJECTION_KIND,
    }
    if extra:
        payload.update(extra)
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _summary_to_entry(summary: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    entries = _build_review_entries(summary=str(summary.get("summary") or ""))
    if not entries:
        return None
    kind, text, extra = entries[0]
    return kind, text, _source_backed_payload(kind, text, summary, extra)


def _person_fact_to_entry(fact: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    entries = _build_review_entries(summary="", person_facts=[fact])
    if not entries:
        return None
    kind, text, extra = entries[0]
    return kind, text, _source_backed_payload(kind, text, fact, extra)


def _event_thread_to_entry(event: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    entries = _build_review_entries(summary="", event_threads=[event])
    if not entries:
        return None
    kind, text, extra = entries[0]
    return kind, text, _source_backed_payload(kind, text, event, extra)


def _entry_source_key(entry: tuple[str, str, dict[str, Any]]) -> str:
    _kind, _text, extra = entry
    return str(extra.get("source_key") or "").strip()


def _entry_source_version(entry: tuple[str, str, dict[str, Any]]) -> str:
    _kind, _text, extra = entry
    return str(extra.get("source_version") or "").strip()


def _expected_source_entries(identity_session: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    entries: list[tuple[str, str, dict[str, Any]]] = []
    for summary in _load_all_summaries_from_sqlite():
        entry = _summary_to_entry(summary)
        if entry:
            entries.append(entry)
    for fact in get_person_facts(include_relationships=True):
        entry = _person_fact_to_entry(fact)
        if entry:
            entries.append(entry)
    for event in _load_all_event_threads_from_sqlite():
        entry = _event_thread_to_entry(event)
        if entry:
            entries.append(entry)
    return entries


def _lookup_source_entry(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    source_type = str(payload.get("source_type") or payload.get("kind") or "").strip()
    source_key = str(payload.get("source_key") or "").strip()
    if not source_type or not source_key:
        return None
    if source_type == "summary":
        row = _load_summary_by_source_key(source_key)
        return _summary_to_entry(row) if row else None
    if source_type == "person_fact":
        row = _load_person_fact_by_source_key(source_key)
        return _person_fact_to_entry(row) if row else None
    if source_type == "event_thread":
        row = _load_event_thread_by_source_key(source_key)
        return _event_thread_to_entry(row) if row else None
    return None


def _entries_with_source_metadata(
    entries: list[tuple[str, str, dict[str, Any]]],
    *,
    context_session: str,
    start_msg_id: int,
    end_msg_id: int,
    summary: str,
    person_facts: list[dict] | None,
    event_threads: list[dict] | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    facts = [item for item in (person_facts or []) if isinstance(item, dict)]
    events = [item for item in (event_threads or []) if isinstance(item, dict)]
    fact_index = 0
    event_index = 0
    for kind, text, extra in entries:
        source: dict[str, Any] = {}
        if kind == "summary":
            source = {
                "session_id": context_session,
                "summary": summary,
                "start_msg_id": start_msg_id,
                "end_msg_id": end_msg_id,
            }
            persisted = _load_summary_by_source_key(
                _summary_source_key(context_session, start_msg_id, end_msg_id)
            )
            if persisted:
                source.update(persisted)
        elif kind == "person_fact":
            while fact_index < len(facts):
                candidate = facts[fact_index]
                fact_index += 1
                if str(candidate.get("fact_key") or candidate.get("key") or "").strip() and str(
                    candidate.get("fact_value") or candidate.get("value") or ""
                ).strip():
                    source = candidate
                    break
        elif kind == "event_thread":
            while event_index < len(events):
                candidate = events[event_index]
                event_index += 1
                if str(candidate.get("thread_key") or candidate.get("key") or "").strip():
                    source = candidate
                    break
        payload_extra = dict(extra or {})
        if kind in SOURCE_BACKED_KINDS and source:
            payload_extra.update(_source_backed_payload(kind, text, source, extra))
        out.append((kind, text, payload_extra))
    return out


def _dedupe_entries_by_source_key(
    entries: list[tuple[str, str, dict[str, Any]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    positions: dict[str, int] = {}
    for entry in entries:
        source_key = _entry_source_key(entry)
        if source_key:
            position = positions.get(source_key)
            if position is not None:
                out[position] = entry
                continue
            positions[source_key] = len(out)
        out.append(entry)
    return out


async def _delete_memu_items(
    service: Any,
    *,
    identity_session: str,
    item_ids: list[str],
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    user_scope = _scope(identity_session, identity_session)
    for item_id in item_ids:
        if not item_id:
            continue
        try:
            await service.delete_memory_item(memory_id=item_id, user=user_scope)
            deleted += 1
        except Exception as exc:
            failed += 1
            _log(
                "delete item failed "
                f"identity={identity_session} item_id={item_id} "
                f"error={type(exc).__name__}: {_preview(exc, 500)}"
            )
    return deleted, failed


async def _delete_existing_source_cards(
    service: Any,
    *,
    identity_session: str,
    source_keys: set[str],
) -> tuple[int, int]:
    if not source_keys:
        return 0, 0
    listed = await service.list_memory_items(where=_global_cache_where())
    delete_ids: list[str] = []
    for item in _items_from_result(listed):
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        payload = _payload_with_extra(item)
        if str(payload.get("source_key") or "").strip() in source_keys:
            delete_ids.append(item_id)
    if not delete_ids:
        return 0, 0
    _log(
        "source cache replace "
        f"identity={identity_session} source_keys={len(source_keys)} delete_existing={len(delete_ids)}"
    )
    return await _delete_memu_items(
        service,
        identity_session=identity_session,
        item_ids=delete_ids,
    )


def _build_review_entries(
    *,
    summary: str,
    person_facts: list[dict] | None = None,
    event_threads: list[dict] | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    entries: list[tuple[str, str, dict[str, Any]]] = []
    character_name = _current_character_name()
    summary_text = " ".join(_replace_default_character_name(summary, character_name).split())
    if summary_text:
        entries.append(("summary", f"对话摘要（用户 / {character_name}）: {summary_text}", {}))
    for fact in person_facts or []:
        if not isinstance(fact, dict):
            continue
        subject = str(
            fact.get("subject_display_name")
            or fact.get("subject")
            or fact.get("subject_person_key")
            or ""
        ).strip()
        obj = str(
            fact.get("object_display_name")
            or fact.get("object")
            or fact.get("object_person_key")
            or ""
        ).strip()
        key = str(fact.get("fact_key") or fact.get("key") or "").strip()
        value = str(fact.get("fact_value") or fact.get("value") or "").strip()
        scope = str(fact.get("scope") or "person").strip()
        if not key or not value:
            continue
        label = subject or "相关人物"
        if scope == "relationship" and obj:
            label = f"{label} ↔ {obj}"
        text = _replace_default_character_name(f"{label} | {key}: {value}", character_name)
        entries.append(
            (
                "person_fact",
                text,
                {
                    "key": key,
                    "subject_person_key": fact.get("subject_person_key"),
                    "object_person_key": fact.get("object_person_key"),
                    "scope": scope,
                },
            )
        )
    for event in event_threads or []:
        event_date = _parse_event_date(event.get("event_time"))
        event_label = _event_date_label(event_date) if event_date else ""
        people_label = str(event.get("people_label") or "").strip()
        title = _replace_default_character_name(
            _absolutize_event_text(str(event.get("title") or "").strip(), event_date),
            character_name,
        )
        details = _replace_default_character_name(
            _absolutize_event_text(str(event.get("details") or "").strip(), event_date),
            character_name,
        )
        followup = _replace_default_character_name(
            _absolutize_event_text(str(event.get("followup_hint") or "").strip(), event_date),
            character_name,
        )
        text_parts = [part for part in (title, details, followup) if part]
        if event_label and event_label not in " ".join(text_parts):
            text_parts.insert(0, event_label)
        event_text = "; ".join(text_parts)
        if event_text:
            if not people_label:
                people_label = f"用户、{character_name}"
            event_text = f"相关人物: {people_label}; {event_text}"
            entries.append(
                (
                    "event_thread",
                    event_text,
                    {
                        "thread_key": event.get("thread_key"),
                        "event_time": event.get("event_time"),
                        "confidence": event.get("confidence"),
                    },
                )
            )
    return entries


def sync_review_memory(
    *,
    context_session: str,
    identity_session: str,
    start_msg_id: int,
    end_msg_id: int,
    summary: str,
    person_facts: list[dict] | None = None,
    event_threads: list[dict] | None = None,
) -> MemuWriteResult:
    _log(
        "sync start "
        f"context={context_session} identity={identity_session} range={start_msg_id}..{end_msg_id} "
        f"summary_chars={len(summary or '')} person_facts={len(person_facts or [])} "
        f"event_threads={len(event_threads or [])}"
    )
    if not is_memu_long_term_enabled():
        _log(
            "sync skipped "
            f"status=disabled context={context_session} identity={identity_session} range={start_msg_id}..{end_msg_id}"
        )
        return MemuWriteResult(status="disabled", ids=[])

    try:
        service = _get_service()
    except Exception as exc:
        _log(
            "sync skipped "
            f"status=unavailable context={context_session} identity={identity_session} "
            f"error={type(exc).__name__}: {_preview(exc, 500)}"
        )
        return MemuWriteResult(status="unavailable", ids=[], error=str(exc))

    entries = _build_review_entries(
        summary=summary,
        person_facts=person_facts,
        event_threads=event_threads,
    )
    entries = _entries_with_source_metadata(
        entries,
        context_session=context_session,
        start_msg_id=start_msg_id,
        end_msg_id=end_msg_id,
        summary=summary,
        person_facts=person_facts,
        event_threads=event_threads,
    )
    entries = _dedupe_entries_by_source_key(entries)
    kind_counts = Counter(kind for kind, _, _ in entries)
    _log(
        "sync entries "
        f"context={context_session} identity={identity_session} total={len(entries)} "
        f"kinds={_json_compact(dict(kind_counts))}"
    )
    if not entries:
        _log(f"sync done status=empty context={context_session} identity={identity_session}")
        return MemuWriteResult(status="empty", ids=[])

    async def _write() -> list[str]:
        ids: list[str] = []
        scope = _scope(identity_session, context_session)
        batch_extra = {
            "resource_type": "pupu_memory_batch",
            "context_session": context_session,
            "identity_session": identity_session,
            "source_msg_start_id": start_msg_id,
            "source_msg_end_id": end_msg_id,
        }
        source_keys = {
            str(extra.get("source_key") or "").strip()
            for _kind, _text, extra in entries
            if str(extra.get("source_key") or "").strip()
        }
        deleted_existing, failed_existing = await _delete_existing_source_cards(
            service,
            identity_session=identity_session,
            source_keys=source_keys,
        )
        if deleted_existing or failed_existing:
            _log(
                "sync source cache replaced "
                f"context={context_session} identity={identity_session} "
                f"deleted={deleted_existing} failed={failed_existing}"
            )

        for index, (kind, text, extra) in enumerate(entries, start=1):
            payload_extra = dict(batch_extra)
            payload_extra.update(extra)
            categories = _categories_for(kind)
            _log(
                "sync item create "
                f"index={index}/{len(entries)} kind={kind} memory_type={_memory_type_for(kind)} "
                f"categories={','.join(categories)} chars={len(text)} "
                f"extra_keys={','.join(sorted(str(key) for key in extra)) or '<none>'} "
                f"text_preview={_preview(text)}"
            )
            result = await service.create_memory_item(
                memory_type=_memory_type_for(kind),
                memory_content=_memory_payload(
                    kind,
                    text,
                    **payload_extra,
                ),
                memory_categories=categories,
                user=scope,
            )
            item_id = _extract_item_id(result)
            result_keys = sorted(result.keys()) if isinstance(result, dict) else [type(result).__name__]
            _log(
                "sync item done "
                f"index={index}/{len(entries)} kind={kind} item_id={item_id or '<missing>'} "
                f"result_keys={','.join(result_keys)}"
            )
            if item_id:
                ids.append(item_id)
        return ids

    start = time.monotonic()
    try:
        with _op_lock:
            ids = _run(_write())
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log(
            "sync success "
            f"context={context_session} identity={identity_session} range={start_msg_id}..{end_msg_id} "
            f"ids_count={len(ids)} ids={_json_compact(ids)} elapsed_ms={elapsed_ms}"
        )
        return MemuWriteResult(status="success", ids=ids)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log(
            "sync failed "
            f"context={context_session} identity={identity_session} range={start_msg_id}..{end_msg_id} "
            f"elapsed_ms={elapsed_ms} error={type(exc).__name__}: {_preview(exc, 800)}"
        )
        return MemuWriteResult(status="failed", ids=[], error=str(exc))


def _items_from_result(result: object) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        items = result.get("items", [])
        return list(items) if isinstance(items, list) else []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def recall_memories(
    *,
    query: str,
    context_session: str,
    identity_session: str,
    history: list[dict] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    requested_limit = limit or _top_k()
    _log(
        "recall start "
        f"context={context_session} identity={identity_session} top_k={requested_limit} "
        f"history_messages={len(history or [])} query_chars={len(query or '')} "
        f"query_preview={_preview(query)}"
    )
    if not is_memu_long_term_enabled():
        _log(f"recall skipped status=disabled context={context_session} identity={identity_session}")
        return []
    try:
        service = _get_service()
    except Exception as exc:
        _log(
            "recall skipped "
            f"status=unavailable context={context_session} identity={identity_session} "
            f"error={type(exc).__name__}: {_preview(exc, 500)}"
        )
        return []

    character_name = _current_character_name()
    recent = _format_history_for_recall(history, character_name=character_name)
    current_query = f"用户: {_replace_default_character_name(query, character_name)}".strip()
    full_query = (recent + "\n" + current_query).strip()
    messages = [{"role": "user", "content": {"text": full_query}}]
    where = _global_cache_where()
    _log(
        "recall request "
        f"context={context_session} identity={identity_session} where={_json_compact(where)} "
        f"full_query_chars={len(full_query)} full_query_preview={_preview(full_query)}"
    )

    async def _retrieve():
        return await service.retrieve(
            queries=messages,
            where=where,
        )

    start = time.monotonic()
    try:
        with _op_lock:
            result = _run(_retrieve())
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log(
            "recall failed "
            f"context={context_session} identity={identity_session} elapsed_ms={elapsed_ms} "
            f"error={type(exc).__name__}: {_preview(exc, 800)}"
        )
        return []

    elapsed_ms = int((time.monotonic() - start) * 1000)
    raw_items = _items_from_result(result)
    result_keys = sorted(result.keys()) if isinstance(result, dict) else [type(result).__name__]
    _log(
        "recall raw "
        f"context={context_session} identity={identity_session} elapsed_ms={elapsed_ms} "
        f"result_keys={','.join(result_keys)} raw_items={len(raw_items)}"
    )

    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        payload = _payload_with_extra(item)
        source_entry = _lookup_source_entry(payload)
        if str(payload.get("projection_kind") or "") == SOURCE_PROJECTION_KIND:
            if source_entry:
                source_kind, source_text, source_extra = source_entry
                payload = {**payload, **source_extra, "kind": source_kind, "text": source_text}
            else:
                _log(
                    "recall item skipped "
                    f"index={index} reason=missing_sqlite_source "
                    f"source_type={payload.get('source_type') or '<none>'} "
                    f"source_key={payload.get('source_key') or '<none>'}"
                )
                continue
        text = _replace_default_character_name(payload.get("text") or "", character_name).strip()
        kind = str(payload.get("kind") or item.get("memory_type") or "other")
        score = item.get("score")
        source_range = ""
        if payload.get("source_msg_start_id") or payload.get("source_msg_end_id"):
            source_range = f"{payload.get('source_msg_start_id', '')}..{payload.get('source_msg_end_id', '')}"
        if not text:
            _log(
                "recall item skipped "
                f"index={index} reason=empty_text kind={kind} raw_keys={','.join(sorted(item.keys()))}"
            )
            continue
        memory = {
            "kind": kind,
            "text": text,
            "source": "memu",
            "score": score,
            "created_at": payload.get("created_at") or item.get("created_at"),
        }
        for meta_key in ("source_type", "source_id", "source_key", "source_version", "projection_kind"):
            if payload.get(meta_key) not in (None, ""):
                memory[meta_key] = payload.get(meta_key)
        out.append(memory)
        score_text = f"{float(score):.4f}" if isinstance(score, (int, float)) else str(score or "")
        _log(
            "recall item "
            f"index={index} accepted={len(out)} kind={kind} score={score_text or '<none>'} "
            f"created_at={memory.get('created_at') or '<none>'} source_range={source_range or '<none>'} "
            f"chars={len(text)} text_preview={_preview(text)}"
        )
        if len(out) >= requested_limit:
            break
    if out:
        _log(f"recall success context={context_session} identity={identity_session} count={len(out)}")
    else:
        _log(f"recall empty context={context_session} identity={identity_session} raw_items={len(raw_items)}")
    return out


def _list_items(identity_session: str, limit: int = 200) -> list[dict[str, Any]]:
    _log(f"list start identity={identity_session} limit={limit}")
    if not is_memu_long_term_enabled():
        _log(f"list skipped status=disabled identity={identity_session}")
        return []
    service = _get_service()
    where = _global_cache_where()

    async def _list():
        return await service.list_memory_items(where=where)

    start = time.monotonic()
    with _op_lock:
        result = _run(_list())
    elapsed_ms = int((time.monotonic() - start) * 1000)
    items = _items_from_result(result)
    limited = list(items)[:limit]
    _log(
        "list done "
        f"identity={identity_session} where={_json_compact(where)} raw_items={len(items)} "
        f"returned={len(limited)} elapsed_ms={elapsed_ms}"
    )
    return limited


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
        "memu_kind_counts": {},
        "missing_keys": [],
        "orphaned_keys": [],
        "duplicate_keys": [],
        "refreshed_keys": [],
        "error": "",
    }


async def _create_source_card(
    service: Any,
    *,
    identity_session: str,
    context_session: str,
    entry: tuple[str, str, dict[str, Any]],
) -> str:
    kind, text, extra = entry
    result = await service.create_memory_item(
        memory_type=_memory_type_for(kind),
        memory_content=_memory_payload(kind, text, **dict(extra or {})),
        memory_categories=_categories_for(kind),
        user=_scope(identity_session, context_session),
    )
    return _extract_item_id(result)


def reconcile_memu_source_cache(
    identity_session: str,
    *,
    context_session: str | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Reconcile memU's rebuildable RAG-card cache with SQLite sources."""
    identity_session = str(identity_session or "default")
    context_session = str(context_session or identity_session)
    mode = "rebuild" if rebuild else ("check" if dry_run else "apply")
    result = _source_card_report_base(mode)

    expected_entries = _dedupe_entries_by_source_key(_expected_source_entries(identity_session))
    expected_by_key = {
        _entry_source_key(entry): entry
        for entry in expected_entries
        if _entry_source_key(entry)
    }
    result["checked"] = len(expected_by_key)
    result["source_kind_counts"] = dict(Counter(kind for kind, _text, _extra in expected_entries))

    if not is_memu_long_term_enabled():
        result["status"] = "disabled"
        result["error"] = "memU disabled"
        return result

    try:
        service = _get_service()
        items = _list_items(identity_session, limit=10000)
    except Exception as exc:
        result["status"] = "unavailable"
        result["error"] = str(exc)
        _log(
            "source cache reconcile skipped "
            f"identity={identity_session} error={type(exc).__name__}: {_preview(exc, 500)}"
        )
        return result

    source_items_by_key: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    source_memu_kind_counts: Counter[str] = Counter()
    for item in items:
        payload = _payload_with_extra(item)
        source_key = str(payload.get("source_key") or "").strip()
        projection_kind = str(payload.get("projection_kind") or "").strip()
        source_type = str(payload.get("source_type") or payload.get("kind") or "").strip()
        if not source_key or projection_kind != SOURCE_PROJECTION_KIND:
            continue
        source_items_by_key.setdefault(source_key, []).append((item, payload))
        source_memu_kind_counts[source_type or "unknown"] += 1
    result["memu_kind_counts"] = dict(source_memu_kind_counts)

    missing_keys = [key for key in expected_by_key if key not in source_items_by_key]
    orphaned_keys = [key for key in source_items_by_key if key not in expected_by_key]
    duplicate_keys = [
        key
        for key, values in source_items_by_key.items()
        if key in expected_by_key and len(values) > 1
    ]
    refreshed_keys = []
    for key, values in source_items_by_key.items():
        if key not in expected_by_key or not values:
            continue
        expected_version = _entry_source_version(expected_by_key[key])
        existing_version = str(values[0][1].get("source_version") or "").strip()
        if expected_version and existing_version != expected_version:
            refreshed_keys.append(key)

    result.update(
        {
            "present": len(expected_by_key) - len(missing_keys),
            "missing": len(missing_keys),
            "orphaned": len(orphaned_keys),
            "duplicates": sum(max(0, len(values) - 1) for values in source_items_by_key.values()),
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

    async def _apply() -> dict[str, int]:
        stats = {"created": 0, "deleted": 0, "failed": 0}
        if rebuild:
            try:
                cleared = await service.clear_memory(where=_global_cache_where())
                deleted_items = cleared.get("deleted_items", []) if isinstance(cleared, dict) else []
                stats["deleted"] += len(deleted_items)
            except Exception as exc:
                _log(
                    "source cache rebuild clear failed; fallback item delete "
                    f"identity={identity_session} error={type(exc).__name__}: {_preview(exc, 500)}"
                )
                delete_ids = [str(item.get("id") or "") for item in items if str(item.get("id") or "")]
                deleted, failed = await _delete_memu_items(
                    service,
                    identity_session=identity_session,
                    item_ids=delete_ids,
                )
                stats["deleted"] += deleted
                stats["failed"] += failed
        else:
            delete_ids: list[str] = []
            for key in orphaned_keys:
                delete_ids.extend(str(item.get("id") or "") for item, _payload in source_items_by_key.get(key, []))
            for key in refreshed_keys:
                delete_ids.extend(str(item.get("id") or "") for item, _payload in source_items_by_key.get(key, []))
            for key in duplicate_keys:
                values = source_items_by_key.get(key, [])
                delete_ids.extend(str(item.get("id") or "") for item, _payload in values[1:])
            delete_ids = list(dict.fromkeys(item_id for item_id in delete_ids if item_id))
            deleted, failed = await _delete_memu_items(
                service,
                identity_session=identity_session,
                item_ids=delete_ids,
            )
            stats["deleted"] += deleted
            stats["failed"] += failed

        create_keys = list(missing_keys)
        if rebuild:
            create_keys = list(expected_by_key)
        else:
            create_keys.extend(refreshed_keys)
        seen_create: set[str] = set()
        for key in create_keys:
            if key in seen_create:
                continue
            seen_create.add(key)
            entry = expected_by_key.get(key)
            if not entry:
                continue
            try:
                item_id = await _create_source_card(
                    service,
                    identity_session=identity_session,
                    context_session=context_session,
                    entry=entry,
                )
            except Exception as exc:
                stats["failed"] += 1
                _log(
                    "source cache create failed "
                    f"identity={identity_session} source_key={key} "
                    f"error={type(exc).__name__}: {_preview(exc, 500)}"
                )
                continue
            if item_id:
                stats["created"] += 1
        return stats

    with _op_lock:
        stats = _run(_apply())
    result["created"] = int(stats.get("created") or 0)
    result["deleted"] = int(stats.get("deleted") or 0)
    result["failed"] = int(stats.get("failed") or 0)
    if result["failed"]:
        result["status"] = "partial"
    elif missing_keys or orphaned_keys or duplicate_keys or refreshed_keys or rebuild:
        result["status"] = "synced"
    else:
        result["status"] = "ok"
    _log(
        "source cache reconcile done "
        f"identity={identity_session} mode={mode} checked={result['checked']} "
        f"missing={result['missing']} orphaned={result['orphaned']} "
        f"duplicates={result['duplicates']} refreshed={result['refreshed']} "
        f"created={result['created']} deleted={result['deleted']} failed={result['failed']} "
        f"status={result['status']}"
    )
    return result


def rebuild_memu_source_cache(
    identity_session: str,
    *,
    context_session: str | None = None,
) -> dict[str, Any]:
    return reconcile_memu_source_cache(
        identity_session,
        context_session=context_session,
        dry_run=False,
        rebuild=True,
    )


def sync_missing_memu_event_threads(
    identity_session: str,
    events: list[dict[str, Any]],
    *,
    context_session: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    result = reconcile_memu_source_cache(
        identity_session,
        context_session=context_session,
        dry_run=dry_run,
        rebuild=False,
    )
    result.setdefault("synced", int(result.get("created") or 0))
    return result


def _format_items(identity_session: str, kinds: set[str], empty_text: str) -> str:
    try:
        items = _list_items(identity_session)
    except Exception as exc:
        _log(f"report failed identity={identity_session} kinds={','.join(sorted(kinds))} error={exc}")
        return f"memU 记忆读取失败：{exc}"

    rows = []
    for item in items:
        payload = _parse_memory_payload(item.get("summary") or item.get("content"))
        item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        payload_extra = item_extra.get("pupu_payload_extra") if isinstance(item_extra, dict) else None
        if isinstance(payload_extra, dict):
            payload = {**payload_extra, **payload}
        if str(payload.get("kind") or "") not in kinds:
            continue
        rows.append((payload, item))

    _log(
        "report prepared "
        f"identity={identity_session} kinds={','.join(sorted(kinds))} matched={len(rows)} raw_items={len(items)}"
    )
    if not rows:
        return empty_text

    lines = [f"memU 长期记忆 {len(rows)} 条"]
    character_name = _current_character_name()
    for index, (payload, item) in enumerate(rows, start=1):
        score = item.get("score")
        score_text = f" score={float(score):.3f}" if isinstance(score, (int, float)) else ""
        text = _replace_default_character_name(payload.get("text") or "", character_name)
        lines.append(f"{index}. [{payload.get('kind')}] {text}{score_text}")
    return "\n".join(lines)


def format_memu_facts_report(identity_session: str) -> str | None:
    if not is_memu_long_term_enabled():
        return None
    return _format_items(identity_session, {"person_fact"}, "当前 memU 里没有 facts 记忆。")


def format_memu_event_threads_report(identity_session: str) -> str | None:
    if not is_memu_long_term_enabled():
        return None
    return _format_items(identity_session, {"event_thread"}, "当前 memU 里没有事件线记忆。")


def format_memu_recall_report(query: str, identity_session: str, context_session: str | None = None) -> str:
    memories = recall_memories(
        query=query,
        context_session=context_session or identity_session,
        identity_session=identity_session,
        history=[],
        limit=_top_k(),
    )
    if not memories:
        return "没有从 memU 召回到相关记忆。"
    lines = [f"memU 召回 {len(memories)} 条"]
    for index, item in enumerate(memories, start=1):
        score = item.get("score")
        meta = f" score={float(score):.3f}" if isinstance(score, (int, float)) else ""
        lines.append(f"{index}. [{item.get('kind')}] {item.get('text')}{meta}")
    return "\n".join(lines)


def clear_memu_session(identity_session: str) -> int:
    _log(f"clear start identity={identity_session}")
    if not is_memu_long_term_enabled():
        _log(f"clear skipped status=disabled identity={identity_session}")
        return 0
    try:
        service = _get_service()
    except Exception as exc:
        _log(f"clear skipped status=unavailable identity={identity_session} error={type(exc).__name__}: {exc}")
        return 0

    where = _global_cache_where()
    user_scope = _scope(identity_session, identity_session)

    async def _clear_all():
        result = await service.clear_memory(where=where)
        deleted_items = result.get("deleted_items", []) if isinstance(result, dict) else []
        deleted_categories = result.get("deleted_categories", []) if isinstance(result, dict) else []
        deleted_resources = result.get("deleted_resources", []) if isinstance(result, dict) else []
        return {
            "deleted": len(deleted_items),
            "deleted_items": len(deleted_items),
            "deleted_categories": len(deleted_categories),
            "deleted_resources": len(deleted_resources),
        }

    async def _clear_items():
        listed = await service.list_memory_items(where=where)
        items = _items_from_result(listed)
        deleted = 0
        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            await service.delete_memory_item(memory_id=item_id, user=user_scope)
            deleted += 1
        return {"listed": len(items), "deleted": deleted}

    start = time.monotonic()
    try:
        with _op_lock:
            result = _run(_clear_all())
        clear_mode = "all"
    except Exception as exc:
        _log(
            "clear all failed; fallback to item-only "
            f"identity={identity_session} error={type(exc).__name__}: {_preview(exc, 500)}"
        )
        with _op_lock:
            result = _run(_clear_items())
        clear_mode = "items_only"
    elapsed_ms = int((time.monotonic() - start) * 1000)
    total = int(result.get("deleted") or 0) if isinstance(result, dict) else 0
    _log(
        "clear done "
        f"identity={identity_session} listed={result.get('listed', 0) if isinstance(result, dict) else 0} "
        f"deleted={total} mode={clear_mode} "
        f"deleted_categories={result.get('deleted_categories', 0) if isinstance(result, dict) else 0} "
        f"deleted_resources={result.get('deleted_resources', 0) if isinstance(result, dict) else 0} "
        f"elapsed_ms={elapsed_ms} result_keys="
        f"{','.join(sorted(result.keys())) if isinstance(result, dict) else type(result).__name__}"
    )
    return total


# Judge-driven memU tidy lives in memu_tidy.py; keep these public names as the
# maintenance entrypoint for callers that import from this adapter module.
def analyze_memu_maintenance(
    identity_session: str,
    *,
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    from .memu_tidy import analyze_memu_tidy

    return analyze_memu_tidy(identity_session, now=now, expire_days=expire_days)


def run_memu_maintenance(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
    trigger: str = "manual",
) -> str:
    from .memu_tidy import format_memu_tidy_report, run_memu_tidy

    result = run_memu_tidy(identity_session, mode=mode, now=now, expire_days=expire_days)
    return format_memu_tidy_report(
        result,
        identity_session=identity_session,
        mode=mode,
        trigger=trigger,
    )
