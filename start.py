"""Instance-first launcher for PuPu."""

from __future__ import annotations

import asyncio
import sys

from pupu.actor import InstanceActor
from pupu.app_config import apply_app_config_env, default_instance_settings, ensure_app_config_file
from pupu.llm import ProviderConfigError, preflight_model_providers
from pupu_console import instance_store


def _load_global_config() -> None:
    cfg_path, created = ensure_app_config_file()
    if created:
        print(f"[pupu] created config file: {cfg_path}")
        print("Fill llm.*.api_key in pupu.yaml before chatting or starting QQ.")
        print()
    apply_app_config_env()


def _read_instance_label(instance_id: str) -> str:
    cfg, _persona = instance_store.read_instance_files(instance_id)
    name = str(cfg.get("display_name") or instance_id)
    mode = str(cfg.get("qq_mode") or "napcat")
    port = int(cfg.get("port") or instance_store.read_port(instance_store.instance_dir(instance_id)))
    return f"{name}  id={instance_id}  mode={mode}  port={port}"


def _create_instance_interactively() -> str:
    defaults = default_instance_settings()
    default_name = defaults["display_name"]
    default_mode = defaults["qq_mode"]
    default_mode_choice = {"cli": "1", "napcat": "2"}.get(default_mode, "2")
    print()
    print("Create PuPu instance")
    print("-" * 40)
    display_name = input(f"  Display name (default: {default_name}): ").strip() or default_name
    print()
    print("  [1] CLI chat")
    print("  [2] QQ via NapCat")
    mode_choice = input(f"  Instance mode (default {default_mode_choice}): ").strip() or default_mode_choice
    qq_mode = {"1": "cli", "2": "napcat"}.get(mode_choice)
    if qq_mode is None:
        print(f"[ERROR] invalid mode: {mode_choice}")
        sys.exit(1)
    default_port = int(defaults["port"])
    port_raw = input(f"  NapCat port (default {default_port}; auto-increments if used): ").strip()
    port = int(port_raw) if port_raw else None
    instance_id = instance_store.create_instance(display_name, port=port, qq_mode=qq_mode)
    print(f"[OK] created instance: {_read_instance_label(instance_id)}")
    return instance_id


def _select_instance_interactively() -> str:
    while True:
        instance_ids = instance_store.list_instance_ids()
        print("=" * 40)
        print("  PuPu Instance Launcher")
        print("=" * 40)
        print()
        if instance_ids:
            for index, instance_id in enumerate(instance_ids, start=1):
                try:
                    print(f"  [{index}] {_read_instance_label(instance_id)}")
                except Exception:
                    print(f"  [{index}] {instance_id}  [failed to read]")
            print()
            print("  [N] Create new instance")
            print("  [Q] Quit")
            choice = input("  Select instance: ").strip()
            if choice.lower() in {"q", "quit", "exit"}:
                sys.exit(0)
            if choice.lower() in {"n", "new", "create", "c"}:
                return _create_instance_interactively()
            if choice.isdigit():
                index = int(choice)
                if 1 <= index <= len(instance_ids):
                    return instance_ids[index - 1]
            if choice in instance_ids:
                return choice
            print(f"[ERROR] invalid selection: {choice}")
            print()
            continue
        print("  No instances yet. Create one first.")
        print()
        return _create_instance_interactively()


def _choose_runtime(config: dict) -> str:
    configured = str(config.get("qq_mode") or "cli").strip().lower()
    if configured == "cli":
        return "cli"
    print()
    print("Run Mode")
    print("-" * 40)
    print("  [1] Start QQ via instance NapCat config")
    print("  [2] Temporarily enter CLI chat")
    choice = input("  Select (default 1): ").strip() or "1"
    if choice == "2":
        return "cli"
    return "qq"


def _require_chat_provider_ready() -> None:
    try:
        preflight_model_providers(require_chat=True)
    except ProviderConfigError as exc:
        print()
        print("[CONFIG ERROR] model provider is not ready:")
        print(exc)
        print()
        print("Open pupu.yaml and fill llm.*.api_key first.")
        sys.exit(2)


async def _run_actor_qq(instance_id: str) -> None:
    actor = InstanceActor.from_instance_dir(instance_store.instance_dir(instance_id), preflight=False)
    await actor.start()
    try:
        cfg, _ = instance_store.read_instance_files(instance_id)
        port = int(cfg.get("port") or instance_store.read_port(instance_store.instance_dir(instance_id)))
        print()
        print("PuPu actor started, waiting for NapCat connection if not already connected.")
        print("Configure NapCat reverse WebSocket as:")
        print(f"  ws://127.0.0.1:{port}/onebot/v11/ws")
        print("Press Ctrl+C to stop.")
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await actor.stop()


def main() -> None:
    _load_global_config()
    instance_id = _select_instance_interactively()
    cfg, _persona = instance_store.read_instance_files(instance_id)
    runtime = _choose_runtime(cfg)
    _require_chat_provider_ready()
    if runtime == "cli":
        from pupu.cli import main as cli_main

        cli_main()
        return
    asyncio.run(_run_actor_qq(instance_id))


if __name__ == "__main__":
    main()
