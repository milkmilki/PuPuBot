"""Rebuild PuPu semantic index cards for local instances.

This operations script loads ``pupu.yaml`` and rebuilds ``semantic_cards`` from
each instance's SQLite facts, summaries, and event threads. SQLite
``data/pupu.db`` remains the source of truth and is never deleted by this
script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


def _bootstrap_imports() -> None:
    sys.path.insert(0, str(REPO_ROOT))


def _discover_instance_dirs(instances_dir: Path, *, include_hidden: bool) -> list[Path]:
    if not instances_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(instances_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") and not include_hidden:
            continue
        if (child / "instance.json").is_file():
            out.append(child)
    return out


def _semantic_card_counts() -> dict[str, int]:
    from pupu.storage.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT source_type, COUNT(*) AS count FROM semantic_cards GROUP BY source_type"
        ).fetchall()
        return {str(row["source_type"]): int(row["count"]) for row in rows}
    finally:
        conn.close()


def _rebuild_instance(instance_dir: Path) -> dict[str, Any]:
    from pupu.instance_context import InstanceContext
    from pupu.semantic_index import run_semantic_tidy
    from pupu.storage.db import init_db

    ctx = InstanceContext.from_instance_dir(instance_dir)
    with ctx.activate():
        init_db()
        result = run_semantic_tidy("owner", mode="rebuild")
        card_counts = _semantic_card_counts()
    return {
        "instance_id": ctx.instance_id,
        "display_name": ctx.display_name,
        "db_path": str(ctx.db_path),
        "result": result,
        "semantic_cards": card_counts,
    }


def _load_config(config_path: Path | None) -> None:
    from pupu.app_config import apply_app_config_env, ensure_app_config_file

    if config_path is None:
        ensure_app_config_file()
    else:
        ensure_app_config_file(config_path)
    apply_app_config_env(
        override=True,
        path=config_path,
        ensure_file=False,
        refresh_tools=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild PuPu semantic_cards for all local instances.",
    )
    parser.add_argument(
        "--instances-dir",
        type=Path,
        default=REPO_ROOT / "instances",
        help="Directory containing instance folders. Defaults to ./instances.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional pupu.yaml path. Defaults to the repository pupu.yaml.",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Also rebuild instance folders whose names start with '_'.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON only.",
    )
    args = parser.parse_args(argv)

    _bootstrap_imports()

    from pupu.semantic_index import is_semantic_index_enabled

    instances_dir = args.instances_dir.expanduser().resolve()
    _load_config(args.config.expanduser().resolve() if args.config else None)
    if not is_semantic_index_enabled():
        message = (
            "semantic index is disabled or missing semantic_index.embed_api_key; "
            "cannot rebuild embeddings"
        )
        if args.json:
            print(json.dumps({"status": "disabled", "error": message}, ensure_ascii=False))
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        return 2

    instance_dirs = _discover_instance_dirs(instances_dir, include_hidden=args.include_hidden)
    results = [_rebuild_instance(instance_dir) for instance_dir in instance_dirs]
    failed = [
        item
        for item in results
        if str(item.get("result", {}).get("status") or "") not in {"synced", "ok"}
    ]

    payload = {
        "status": "failed" if failed else "ok",
        "instances_dir": str(instances_dir),
        "instances": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
