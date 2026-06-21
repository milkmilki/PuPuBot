"""Entry point for a single PuPu instance (subprocess / multi-instance console).

Expects ``--dir`` to point at a directory containing ``instance.json``,
``persona.json``, ``.env.qq``, and ``data/``.

The process working directory is forced to the repository root so NoneBot can
``load_plugins("plugins")`` reliably.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from pupu.app_config import apply_app_config_env, ensure_app_config_file
from pupu.instance_context import InstanceContext, activate_instance_context_global

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one PuPu instance.")
    parser.add_argument(
        "--dir",
        dest="instance_dir",
        required=True,
        help="Instance directory.",
    )
    return parser.parse_args(argv)


def _ensure_instance_env(inst: Path) -> None:
    ctx = InstanceContext.from_instance_dir(inst)
    activate_instance_context_global(ctx)
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)


def _load_instance_dotenv(inst: Path) -> None:
    env_file = inst / ".env.qq"
    if env_file.is_file():
        load_dotenv(env_file, override=True)


def _read_instance_config(inst: Path) -> dict:
    path = inst / "instance.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing instance.json: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_ws_port(inst: Path) -> int:
    env_path = inst / ".env.qq"
    if not env_path.is_file():
        return 8081
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PORT="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            try:
                return int(raw)
            except ValueError:
                break
    return 8081


def _run_qq_bot(config: dict, env_file: Path) -> None:
    import nonebot

    qq_mode = config.get("qq_mode", "napcat")

    if qq_mode == "napcat":
        nonebot.init(_env_file=str(env_file), driver="~fastapi")
        from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

        driver = nonebot.get_driver()
        driver.register_adapter(OneBotV11Adapter)
        port = _read_ws_port(env_file.parent)
        print("[PuPu QQ] mode: NapCat (OneBot v11)")
        print()
        print("  PuPu started, waiting for NapCat connection...")
        print("  Make sure NapCat reverse WebSocket is configured as:")
        print(f"    ws://127.0.0.1:{port}/onebot/v11/ws")
        print()

    elif qq_mode == "official":
        app_id = config.get("qq_app_id", "")
        app_secret = config.get("qq_app_secret", "")
        nonebot.init(
            _env_file=str(env_file),
            driver="~httpx+~websockets",
            qq_bots=[
                {
                    "id": app_id,
                    "token": "",
                    "secret": app_secret,
                    "intent": {
                        "c2c_group_at_messages": True,
                    },
                }
            ],
        )
        from nonebot.adapters.qq import Adapter as QQAdapter

        driver = nonebot.get_driver()
        driver.register_adapter(QQAdapter)
        print(f"[PuPu QQ] mode: QQ official bot (AppID: {app_id})")

    else:
        print(
            f"[error] unknown qq_mode: {qq_mode!r} (supported: napcat / official / cli)",
            file=sys.stderr,
        )
        sys.exit(1)

    nonebot.load_plugins("plugins")
    nonebot.run()


def main(argv: list[str] | None = None) -> None:
    ensure_app_config_file()
    apply_app_config_env()
    args = _parse_args(argv)

    inst = Path(args.instance_dir).resolve()
    if not inst.is_dir():
        print(f"Instance directory does not exist: {inst}", file=sys.stderr)
        sys.exit(2)

    _ensure_instance_env(inst)
    _load_instance_dotenv(inst)
    os.chdir(REPO_ROOT)

    from pupu.llm import ProviderConfigError, preflight_model_providers
    from pupu.logging_utils import setup_runtime_logging

    setup_runtime_logging()
    config = _read_instance_config(inst)
    try:
        preflight_model_providers(require_chat=True)
    except ProviderConfigError as exc:
        print("[config error] model provider is not usable yet:", file=sys.stderr)
        print(exc, file=sys.stderr)
        print(
            "Please open pupu.yaml and fill the relevant llm.*.api_key settings.",
            file=sys.stderr,
        )
        sys.exit(2)

    qq_mode = config.get("qq_mode", "napcat")
    env_file = inst / ".env.qq"

    if qq_mode == "cli":
        from pupu.cli import main as cli_main

        cli_main()
        return

    _run_qq_bot(config, env_file)


if __name__ == "__main__":
    main()
