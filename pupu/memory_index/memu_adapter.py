"""memU-backed long-term memory index for PuPu."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..persona.core import get_pupu_name
from ..storage.db import get_conn, get_data_dir

DEFAULT_TOP_K = 6
DEFAULT_LOG_PREVIEW_CHARS = 220
DEFAULT_RECENCY_DECAY_DAYS = 30.0
_service = None
_service_error: str | None = None
_service_lock = threading.Lock()
_op_lock = threading.Lock()
_disabled_reasons_logged: set[str] = set()
_config_logged = False
_sqlite_backend_patched = False


def _current_character_name() -> str:
    return get_pupu_name().strip() or "仆仆"


def _replace_default_character_name(text: str, character_name: str | None = None) -> str:
    value = str(text or "")
    name = str(character_name or _current_character_name()).strip()
    if name and name != "仆仆":
        value = value.replace("仆仆", name)
    return value


def _speaker_label(role: object, character_name: str | None = None) -> str:
    return "用户" if str(role or "") == "user" else str(character_name or _current_character_name())


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


TARGET_MAINTENANCE_KINDS = {"user_fact", "self_fact", "important_event"}
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
    stable_keys = ("kind", "text", "key", "source_event_key", "event_time", "confidence")
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
    configured = os.environ.get("PUPU_MEMU_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(get_data_dir()) / "memu.db"


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
    raw = os.environ.get("PUPU_MEMU_NATIVE_CATEGORY_SUMMARIES", "true").strip().lower()
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
    global _config_logged
    if _config_logged:
        return
    profiles = _llm_profiles()
    default = profiles["default"]
    embedding = profiles["embedding"]
    _log(
        "config "
        f"reason={reason} enabled_env={_env_bool_auto('PUPU_MEMU_ENABLED', 'auto')} "
        f"db={_memu_db_path()} method={os.environ.get('PUPU_MEMU_METHOD', 'rag')} top_k={_top_k()} "
        f"ranking={_memu_ranking()} recency_decay_days={_recency_decay_days()} "
        f"reinforcement={'yes' if _enable_reinforcement() else 'no'} "
        f"native_category_summaries={'yes' if _native_category_summaries() else 'no'} "
        f"llm_provider={default.get('provider')} llm_base_url={default.get('base_url')} "
        f"llm_model={default.get('chat_model')} llm_key={'yes' if default.get('api_key') != 'missing' else 'no'} "
        f"embed_provider={embedding.get('provider')} embed_base_url={embedding.get('base_url')} "
        f"embed_model={embedding.get('embed_model')} embed_key={'yes' if _configured_embedding_key() else 'no'} "
        f"embed_key_source={_configured_embedding_key_name() or '<none>'}"
    )
    _config_logged = True


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


def _get_service():
    global _service, _service_error
    if _service is not None:
        return _service
    if _service_error is not None:
        raise RuntimeError(_service_error)
    with _service_lock:
        if _service is not None:
            return _service
        if _service_error is not None:
            raise RuntimeError(_service_error)
        if not is_memu_long_term_enabled():
            raise RuntimeError("memU long-term memory is disabled")
        if not _configured_embedding_key():
            raise RuntimeError("memU embedding API key is not configured")
        try:
            _log_config_once("service_init")
            _log("service init start")
            _service = _new_service()
            _service_error = None
            _log(f"service enabled db={_memu_db_path()}")
            return _service
        except Exception as exc:
            _service_error = str(exc)
            _log(f"service unavailable error={type(exc).__name__}: {_preview(exc, 500)}")
            raise


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


def _memory_payload(kind: str, text: str, **extra: Any) -> str:
    payload = {
        "kind": kind,
        "text": " ".join(str(text or "").split()),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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


def _load_legacy_event_map(identity_session: str) -> dict[str, dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT source_event_key, kind, event_time, status, linked_task_id,
                      created_at, title, details, followup_hint
               FROM important_events
               WHERE session_id = ?""",
            (identity_session,),
        ).fetchall()
        return {str(row["source_event_key"]): dict(row) for row in rows}
    finally:
        conn.close()


def _legacy_source_action(candidate: dict[str, Any]) -> str:
    if not candidate.get("delete_legacy"):
        return "none"
    table = candidate.get("legacy_table") or ""
    key = candidate.get("legacy_key") or ""
    return f"delete {table}:{key}" if table and key else "none"


def _delete_legacy_source(identity_session: str, candidate: dict[str, Any]) -> int:
    if not candidate.get("delete_legacy"):
        return 0
    table = str(candidate.get("legacy_table") or "")
    key = str(candidate.get("legacy_key") or "")
    if table not in {"user_facts", "self_facts", "important_events"} or not key:
        return 0
    conn = get_conn()
    try:
        if table in {"user_facts", "self_facts"}:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE session_id = ? AND fact_key = ?",
                (identity_session, key),
            )
        else:
            cur = conn.execute(
                "DELETE FROM important_events WHERE session_id = ? AND source_event_key = ?",
                (identity_session, key),
            )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


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
    if kind == "user_fact":
        return ["personal_info", "preferences"]
    if kind == "self_fact":
        return ["relationships", "knowledge"]
    if kind == "important_event":
        return ["experiences", "goals", "relationships"]
    if kind == "summary":
        return ["experiences"]
    return ["knowledge"]


def _memory_type_for(kind: str) -> str:
    if kind == "important_event":
        return "event"
    if kind in {"user_fact", "self_fact"}:
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


def _build_review_entries(
    *,
    summary: str,
    user_facts: dict[str, str] | None = None,
    self_facts: dict[str, str] | None = None,
    important_events: list[dict] | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    entries: list[tuple[str, str, dict[str, Any]]] = []
    character_name = _current_character_name()
    summary_text = " ".join(_replace_default_character_name(summary, character_name).split())
    if summary_text:
        entries.append(("summary", f"对话摘要（用户 / {character_name}）: {summary_text}", {}))
    for key, value in (user_facts or {}).items():
        text = _replace_default_character_name(f"{key}: {value}", character_name)
        entries.append(("user_fact", f"用户 | {text}", {"key": key}))
    for key, value in (self_facts or {}).items():
        text = _replace_default_character_name(f"{key}: {value}", character_name)
        entries.append(("self_fact", f"{character_name} | {text}", {"key": key}))
    for event in important_events or []:
        event_date = _parse_event_date(event.get("event_time"))
        event_label = _event_date_label(event_date) if event_date else ""
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
            event_text = f"相关人物: 用户、{character_name}; {event_text}"
            entries.append(
                (
                    "important_event",
                    event_text,
                    {
                        "source_event_key": event.get("source_event_key"),
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
    user_facts: dict[str, str] | None = None,
    self_facts: dict[str, str] | None = None,
    important_events: list[dict] | None = None,
) -> MemuWriteResult:
    _log(
        "sync start "
        f"context={context_session} identity={identity_session} range={start_msg_id}..{end_msg_id} "
        f"summary_chars={len(summary or '')} user_facts={len(user_facts or {})} "
        f"self_facts={len(self_facts or {})} important_events={len(important_events or [])}"
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
        user_facts=user_facts,
        self_facts=self_facts,
        important_events=important_events,
    )
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
    where = {"identity_session": identity_session}
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
        payload = _parse_memory_payload(item.get("summary") or item.get("content"))
        item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        payload_extra = item_extra.get("pupu_payload_extra") if isinstance(item_extra, dict) else None
        if isinstance(payload_extra, dict):
            payload = {**payload_extra, **payload}
        text = str(payload.get("text") or "").strip()
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
    where = {"identity_session": identity_session}

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
    for index, (payload, item) in enumerate(rows, start=1):
        score = item.get("score")
        score_text = f" score={float(score):.3f}" if isinstance(score, (int, float)) else ""
        lines.append(f"{index}. [{payload.get('kind')}] {payload.get('text')}{score_text}")
    return "\n".join(lines)


def format_memu_facts_report(identity_session: str) -> str | None:
    if not is_memu_long_term_enabled():
        return None
    return _format_items(identity_session, {"user_fact", "self_fact"}, "当前 memU 里没有 facts 记忆。")


def format_memu_important_events_report(identity_session: str) -> str | None:
    if not is_memu_long_term_enabled():
        return None
    return _format_items(identity_session, {"important_event"}, "当前 memU 里没有重要事件记忆。")


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

    where = {"identity_session": identity_session}

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
            await service.delete_memory_item(memory_id=item_id, user=where)
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


def rebuild_memu_session(identity_session: str, context_session: str | None = None) -> str:
    _log(f"rebuild start identity={identity_session} context={context_session or identity_session}")
    if not is_memu_long_term_enabled():
        _log(f"rebuild skipped status=disabled identity={identity_session}")
        return "memU 未启用，无法重建。"

    identity_session = str(identity_session)
    context_session = str(context_session or identity_session)
    removed = clear_memu_session(identity_session)
    conn = get_conn()
    try:
        summaries = [
            dict(row)
            for row in conn.execute(
                """SELECT summary, start_msg_id, end_msg_id, created_at
                   FROM summaries
                   WHERE session_id = ?
                   ORDER BY created_at ASC, id ASC""",
                (context_session,),
            ).fetchall()
        ]
        user_facts = {
            row["fact_key"]: row["fact_value"]
            for row in conn.execute(
                "SELECT fact_key, fact_value FROM user_facts WHERE session_id = ?",
                (identity_session,),
            ).fetchall()
        }
        self_facts = {
            row["fact_key"]: row["fact_value"]
            for row in conn.execute(
                "SELECT fact_key, fact_value FROM self_facts WHERE session_id = ?",
                (identity_session,),
            ).fetchall()
        }
        events = [
            dict(row)
            for row in conn.execute(
                """SELECT source_event_key, title, kind, event_time, time_text,
                          details, followup_hint, confidence
                   FROM important_events
                   WHERE session_id = ?
                   ORDER BY created_at ASC, id ASC""",
                (identity_session,),
            ).fetchall()
        ]
    finally:
        conn.close()

    _log(
        "rebuild loaded "
        f"identity={identity_session} context={context_session} removed={removed} "
        f"summaries={len(summaries)} user_facts={len(user_facts)} self_facts={len(self_facts)} "
        f"important_events={len(events)}"
    )
    ids: list[str] = []
    failures = 0
    for index, row in enumerate(summaries, start=1):
        summary_text = str(row.get("summary") or "").strip()
        if not summary_text:
            continue
        start_msg_id = int(row.get("start_msg_id") or 0)
        end_msg_id = int(row.get("end_msg_id") or 0)
        _log(
            "rebuild sync summary "
            f"index={index}/{len(summaries)} context={context_session} "
            f"range={start_msg_id}..{end_msg_id}"
        )
        result = sync_review_memory(
            context_session=context_session,
            identity_session=identity_session,
            start_msg_id=start_msg_id,
            end_msg_id=end_msg_id,
            summary=summary_text,
            user_facts={},
            self_facts={},
            important_events=[],
        )
        ids.extend(result.ids)
        if result.status not in {"success", "empty"}:
            failures += 1

    result = sync_review_memory(
        context_session=context_session,
        identity_session=identity_session,
        start_msg_id=0,
        end_msg_id=0,
        summary="",
        user_facts=user_facts,
        self_facts=self_facts,
        important_events=events,
    )
    ids.extend(result.ids)
    if result.status not in {"success", "empty"}:
        failures += 1
    _log(
        "rebuild done "
        f"identity={identity_session} context={context_session} removed={removed} "
        f"written={len(ids)} failures={failures} summaries={len(summaries)}"
    )
    return (
        f"memU 重建完成：清理 {removed} 项，写入 {len(ids)} 项，"
        f"失败 {failures} 批，迁移旧摘要 {len(summaries)} 条。"
    )


def _maintenance_candidate(
    *,
    item_id: str,
    kind: str,
    reason: str,
    text: str,
    delete_legacy: bool = False,
    legacy_table: str = "",
    legacy_key: str = "",
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "kind": kind,
        "reason": reason,
        "text": text,
        "delete_legacy": delete_legacy,
        "legacy_table": legacy_table,
        "legacy_key": legacy_key,
    }


def _event_is_protected(payload: dict[str, Any], legacy: dict[str, Any] | None, now: datetime) -> bool:
    kind = str((legacy or {}).get("kind") or payload.get("kind") or "").strip().lower()
    if kind in PROTECTED_EVENT_KINDS:
        return True
    status = str((legacy or {}).get("status") or payload.get("status") or "").strip().lower()
    if status == "scheduled":
        return True
    if (legacy or {}).get("linked_task_id") or payload.get("linked_task_id"):
        return True
    event_dt = _parse_datetime((legacy or {}).get("event_time") or payload.get("event_time"))
    if event_dt and event_dt >= now:
        return True
    text = _norm_text(payload.get("text"))
    return any(term in text for term in LONG_TERM_EVENT_TERMS)


def _build_maintenance_candidate(
    item: dict[str, Any],
    payload: dict[str, Any],
    *,
    parse_failed: bool,
    seen: set[tuple[str, str]],
    legacy_events: dict[str, dict[str, Any]],
    now: datetime,
    expire_days: int,
) -> dict[str, Any] | None:
    item_id = str(item.get("id") or "")
    if not item_id:
        return None
    kind = str(payload.get("kind") or item.get("memory_type") or "").strip()
    text = _norm_text(payload.get("text"))
    if kind == "summary":
        return None
    if kind not in TARGET_MAINTENANCE_KINDS:
        if parse_failed and str(item.get("memory_type") or "") in {"profile", "event"}:
            return _maintenance_candidate(
                item_id=item_id,
                kind=kind or str(item.get("memory_type") or "unknown"),
                reason="invalid_payload",
                text=text or str(item.get("summary") or item.get("content") or ""),
            )
        return None

    if parse_failed:
        return _maintenance_candidate(item_id=item_id, kind=kind, reason="invalid_payload", text=text)

    key = (kind, text)
    if key in seen:
        return _maintenance_candidate(item_id=item_id, kind=kind, reason="duplicate", text=text)
    seen.add(key)

    if not text or len(text) < 4:
        legacy_table = "user_facts" if kind == "user_fact" else "self_facts" if kind == "self_fact" else ""
        legacy_key = str(payload.get("key") or payload.get("source_event_key") or "").strip()
        return _maintenance_candidate(
            item_id=item_id,
            kind=kind,
            reason="low_value",
            text=text,
            delete_legacy=bool(legacy_table and legacy_key),
            legacy_table=legacy_table,
            legacy_key=legacy_key,
        )

    if kind in {"user_fact", "self_fact"}:
        fact_key, _value = _fact_key_value(payload)
        legacy_table = "user_facts" if kind == "user_fact" else "self_facts"
        if _is_low_info_fact(payload):
            return _maintenance_candidate(
                item_id=item_id,
                kind=kind,
                reason="low_info_fact",
                text=text,
                delete_legacy=bool(fact_key),
                legacy_table=legacy_table,
                legacy_key=fact_key,
            )
        if _has_relative_time(text) and not _looks_stable_fact(text):
            return _maintenance_candidate(
                item_id=item_id,
                kind=kind,
                reason="temporary_fact",
                text=text,
                delete_legacy=bool(fact_key),
                legacy_table=legacy_table,
                legacy_key=fact_key,
            )
        return None

    if kind == "important_event":
        source_key = str(payload.get("source_event_key") or "").strip()
        legacy = legacy_events.get(source_key) if source_key else None
        if _event_is_protected(payload, legacy, now):
            return None
        cutoff = now - timedelta(days=max(1, int(expire_days or 14)))
        event_dt = _parse_datetime((legacy or {}).get("event_time") or payload.get("event_time"))
        created_dt = _parse_datetime(payload.get("created_at") or item.get("created_at") or (legacy or {}).get("created_at"))
        if event_dt and event_dt < cutoff:
            return _maintenance_candidate(
                item_id=item_id,
                kind=kind,
                reason="expired_important_event",
                text=text,
                delete_legacy=bool(source_key),
                legacy_table="important_events",
                legacy_key=source_key,
            )
        if not event_dt and created_dt and created_dt < cutoff and _has_relative_time(text):
            return _maintenance_candidate(
                item_id=item_id,
                kind=kind,
                reason="stale_relative_event",
                text=text,
                delete_legacy=bool(source_key),
                legacy_table="important_events",
                legacy_key=source_key,
            )
    return None


def analyze_memu_maintenance(
    identity_session: str,
    *,
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    _log(f"maintenance analyze start identity={identity_session} expire_days={expire_days}")
    if not is_memu_long_term_enabled():
        _log(f"maintenance analyze skipped status=disabled identity={identity_session}")
        return {
            "status": "disabled",
            "scanned": 0,
            "candidates": [],
            "kind_counts": {},
            "reason_counts": {},
            "note": "memU disabled",
        }
    try:
        items = _list_items(identity_session, limit=10000)
    except Exception as exc:
        _log(f"maintenance analyze skipped identity={identity_session} error={type(exc).__name__}: {exc}")
        return {
            "status": "unavailable",
            "scanned": 0,
            "candidates": [],
            "kind_counts": {},
            "reason_counts": {},
            "note": f"memU skipped ({exc})",
        }

    run_at = now or datetime.now()
    legacy_events = _load_legacy_event_map(identity_session)
    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []
    kind_counts: Counter[str] = Counter()
    for item in items:
        payload, parse_failed = _payload_from_item(item)
        kind = str(payload.get("kind") or item.get("memory_type") or "unknown")
        kind_counts[kind] += 1
        candidate = _build_maintenance_candidate(
            item,
            payload,
            parse_failed=parse_failed,
            seen=seen,
            legacy_events=legacy_events,
            now=run_at,
            expire_days=expire_days,
        )
        if candidate:
            candidates.append(candidate)
            _log(
                "maintenance candidate "
                f"identity={identity_session} item_id={candidate['item_id']} "
                f"kind={candidate['kind']} reason={candidate['reason']} "
                f"old_source_action={_legacy_source_action(candidate)} "
                f"text_preview={_preview(candidate['text'])}"
            )

    reason_counts = Counter(str(item["reason"]) for item in candidates)
    _log(
        "maintenance analyze done "
        f"identity={identity_session} scanned={len(items)} candidates={len(candidates)} "
        f"kinds={_json_compact(dict(kind_counts))} reasons={_json_compact(dict(reason_counts))}"
    )
    return {
        "status": "ok",
        "scanned": len(items),
        "candidates": candidates,
        "kind_counts": dict(kind_counts),
        "reason_counts": dict(reason_counts),
        "note": "",
    }


def _format_candidate_preview(candidates: list[dict[str, Any]], limit: int = 5) -> str:
    parts = []
    for item in candidates[:limit]:
        action = _legacy_source_action(item)
        parts.append(
            f"{item.get('kind')}:{item.get('reason')}:{_preview(item.get('text'), 80)}"
            + (f" old={action}" if action != "none" else "")
        )
    return " | ".join(parts)


def run_memu_maintenance(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    _log(f"maintenance start identity={identity_session}")
    mode = str(mode or "apply").strip().lower()
    if mode not in {"apply", "check"}:
        raise ValueError("memU maintenance mode must be 'apply' or 'check'")
    analysis = analyze_memu_maintenance(identity_session, now=now, expire_days=expire_days)
    candidates = list(analysis.get("candidates") or [])
    if analysis.get("status") != "ok":
        return {
            "mode": mode,
            "scanned": int(analysis.get("scanned") or 0),
            "candidates": len(candidates),
            "deleted": 0,
            "failed": 0,
            "legacy_deleted": 0,
            "updated": 0,
            "reason_counts": analysis.get("reason_counts") or {},
            "kind_counts": analysis.get("kind_counts") or {},
            "note": analysis.get("note") or "",
        }
    if mode == "check":
        note = (
            f"memU mode=check, scanned={analysis['scanned']}, candidates={len(candidates)}, "
            f"reasons={analysis['reason_counts']}"
        )
        preview = _format_candidate_preview(candidates)
        if preview:
            note += f", preview={preview}"
        _log(f"maintenance check done identity={identity_session} note={note}")
        return {
            "mode": mode,
            "scanned": int(analysis.get("scanned") or 0),
            "candidates": len(candidates),
            "deleted": 0,
            "failed": 0,
            "legacy_deleted": 0,
            "updated": 0,
            "reason_counts": analysis.get("reason_counts") or {},
            "kind_counts": analysis.get("kind_counts") or {},
            "note": note,
        }

    service = _get_service()

    async def _delete(items_to_delete: list[dict[str, Any]]) -> tuple[int, int, int]:
        deleted = 0
        failed = 0
        legacy_deleted = 0
        for candidate in items_to_delete:
            item_id = str(candidate.get("item_id") or "")
            if not item_id:
                continue
            _log(
                "maintenance delete item "
                f"identity={identity_session} item_id={item_id} kind={candidate.get('kind')} "
                f"reason={candidate.get('reason')} old_source_action={_legacy_source_action(candidate)}"
            )
            try:
                await service.delete_memory_item(memory_id=item_id, user={"identity_session": identity_session})
            except Exception as exc:
                failed += 1
                _log(
                    "maintenance delete failed "
                    f"identity={identity_session} item_id={item_id} error={type(exc).__name__}: {_preview(exc, 500)}"
                )
                continue
            deleted += 1
            try:
                legacy_deleted += _delete_legacy_source(identity_session, candidate)
            except Exception as exc:
                _log(
                    "maintenance legacy delete failed "
                    f"identity={identity_session} item_id={item_id} "
                    f"old_source_action={_legacy_source_action(candidate)} "
                    f"error={type(exc).__name__}: {_preview(exc, 500)}"
                )
        return deleted, failed, legacy_deleted

    deleted = 0
    failed = 0
    legacy_deleted = 0
    if candidates:
        with _op_lock:
            deleted, failed, legacy_deleted = _run(_delete(candidates))
    note = (
        f"memU mode=apply, scanned={analysis['scanned']}, candidates={len(candidates)}, "
        f"reasons={analysis['reason_counts']}, deleted={deleted}, "
        f"legacy_deleted={legacy_deleted}, failed={failed}"
    )
    preview = _format_candidate_preview(candidates)
    if preview:
        note += f", preview={preview}"
    _log(f"maintenance done identity={identity_session} deleted={deleted} updated=0 note={note}")
    return {
        "mode": mode,
        "scanned": int(analysis.get("scanned") or 0),
        "candidates": len(candidates),
        "deleted": deleted,
        "failed": failed,
        "legacy_deleted": legacy_deleted,
        "updated": 0,
        "reason_counts": analysis.get("reason_counts") or {},
        "kind_counts": analysis.get("kind_counts") or {},
        "note": note,
    }


# Compatibility wrappers. The judge-driven memU tidy lives in memu_tidy.py,
# but these names stay available for older import paths.
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
