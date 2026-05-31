"""Entry point for a single PuPu instance (subprocess / multi-instance console).

Expects ``PUPU_INSTANCE_DIR`` (or ``--dir``) to point at a directory containing
``instance.json``, ``persona.json``, ``.env.qq``, and ``data/``.

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

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one PuPu instance.")
    parser.add_argument(
        "--dir",
        dest="instance_dir",
        default=os.environ.get("PUPU_INSTANCE_DIR"),
        help="Instance directory (or set PUPU_INSTANCE_DIR).",
    )
    return parser.parse_args(argv)


def _ensure_instance_env(inst: Path) -> None:
    os.environ["PUPU_INSTANCE_DIR"] = str(inst)
    os.environ.setdefault("PUPU_CONFIG_PATH", str(inst / "instance.json"))
    os.environ.setdefault("PUPU_DB_PATH", str(inst / "data" / "pupu.db"))
    os.environ["PUPU_MEMU_DB_PATH"] = str(inst / "data" / "memu.db")
    os.environ.setdefault("PUPU_PERSONA_PATH", str(inst / "persona.json"))
    (inst / "data").mkdir(parents=True, exist_ok=True)
    (inst / "data" / "logs").mkdir(parents=True, exist_ok=True)


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
        print("[仆仆QQ] 模式: NapCat (OneBot v11)")
        print()
        print("  仆仆已启动，正在等待 NapCat 连接...")
        print("  请确保 NapCat 已配置反向 WebSocket 地址:")
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
        print(f"[仆仆QQ] 模式: QQ 官方机器人 (AppID: {app_id})")

    else:
        print(f"[错误] 未知 qq_mode: {qq_mode!r}（支持 napcat / official / cli）", file=sys.stderr)
        sys.exit(1)

    nonebot.load_plugins("plugins")
    nonebot.run()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if not args.instance_dir:
        print("需要 --dir 或环境变量 PUPU_INSTANCE_DIR", file=sys.stderr)
        sys.exit(2)

    inst = Path(args.instance_dir).resolve()
    if not inst.is_dir():
        print(f"实例目录不存在: {inst}", file=sys.stderr)
        sys.exit(2)

    _ensure_instance_env(inst)
    _load_instance_dotenv(inst)
    os.chdir(REPO_ROOT)

    from pupu.llm import preflight_model_providers
    from pupu.logging_utils import setup_runtime_logging

    setup_runtime_logging()
    config = _read_instance_config(inst)
    preflight_model_providers()

    qq_mode = config.get("qq_mode", "napcat")
    env_file = inst / ".env.qq"

    if qq_mode == "cli":
        from pupu.cli import main as cli_main

        cli_main()
        return

    _run_qq_bot(config, env_file)


if __name__ == "__main__":
    main()
