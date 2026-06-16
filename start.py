"""Instance-first launcher for PuPu.

Every runtime now belongs to ``instances/<id>``. This entry point lets the user
pick an existing instance or create one, then starts either CLI chat or the QQ
bot for that instance.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from pupu.app_config import apply_app_config_env, default_instance_settings, ensure_app_config_file
from pupu.logging_utils import setup_runtime_logging
from pupu_console import instance_store


def _load_global_config() -> None:
    cfg_path, created = ensure_app_config_file()
    if created:
        print(f"[pupu] 已创建默认配置文件：{cfg_path}")
        print("[pupu] 请在其中填写 llm.*.api_key；未填写前不能开始聊天或启动 QQ。")
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
    default_mode_choice = {"cli": "1", "napcat": "2", "official": "3"}.get(default_mode, "1")
    print()
    print("创建新实例")
    print("-" * 40)
    display_name = input(f"  显示名称（默认：{default_name}）: ").strip() or default_name
    print()
    print("  [1] 终端聊天实例")
    print("  [2] QQ 机器人 - NapCat")
    print("  [3] QQ 机器人 - 官方")
    mode_choice = input(f"  实例模式（默认 {default_mode_choice}）: ").strip() or default_mode_choice
    qq_mode = {"1": "cli", "2": "napcat", "3": "official"}.get(mode_choice)
    if qq_mode is None:
        print(f"[错误] 无效模式: {mode_choice}")
        sys.exit(1)

    default_port = int(defaults["port"])
    port_raw = input(f"  端口（默认 {default_port}，若被占用会自动顺延）: ").strip()
    port = int(port_raw) if port_raw else None
    instance_id = instance_store.create_instance(display_name, port=port, qq_mode=qq_mode)

    if qq_mode == "official":
        cfg, persona = instance_store.read_instance_files(instance_id)
        print()
        default_app_id = defaults["qq_app_id"]
        default_app_secret = defaults["qq_app_secret"]
        app_id = input("  QQ AppID（可稍后在 pupu.yaml 或控制台填写）: ").strip() or default_app_id
        app_secret = input("  QQ AppSecret（可稍后在 pupu.yaml 或控制台填写）: ").strip() or default_app_secret
        if app_id or app_secret:
            cfg["qq_app_id"] = app_id
            cfg["qq_app_secret"] = app_secret
            instance_store.write_instance_files(instance_id, cfg, persona, sync_port=False)

    print(f"[OK] 已创建实例：{_read_instance_label(instance_id)}")
    return instance_id


def _select_instance_interactively() -> str:
    while True:
        instance_ids = instance_store.list_instance_ids()
        print("=" * 40)
        print("  仆仆实例启动器")
        print("=" * 40)
        print()
        if instance_ids:
            for index, instance_id in enumerate(instance_ids, start=1):
                try:
                    print(f"  [{index}] {_read_instance_label(instance_id)}")
                except Exception:
                    print(f"  [{index}] {instance_id}  [读取失败]")
            print()
            print("  [N] 创建新实例")
            print("  [Q] 退出")
            choice = input("  请选择实例: ").strip()
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
            print(f"[错误] 无效选择: {choice}")
            print()
            continue

        print("  还没有实例。请先创建一个。")
        print()
        return _create_instance_interactively()


def _apply_instance_env(instance_id: str) -> Path:
    inst = instance_store.instance_dir(instance_id).resolve()
    os.environ["PUPU_INSTANCE_DIR"] = str(inst)
    os.environ["PUPU_CONFIG_PATH"] = str(inst / "instance.json")
    os.environ["PUPU_DB_PATH"] = str(inst / "data" / "pupu.db")
    os.environ["PUPU_MEMU_DB_PATH"] = str(inst / "data" / "memu.db")
    os.environ["PUPU_PERSONA_PATH"] = str(inst / "persona.json")
    (inst / "data").mkdir(parents=True, exist_ok=True)
    (inst / "data" / "logs").mkdir(parents=True, exist_ok=True)
    env_file = inst / ".env.qq"
    if env_file.is_file():
        load_dotenv(env_file, override=True)
    return inst


def _choose_runtime_mode(config: dict) -> str:
    configured = str(config.get("qq_mode") or "cli").strip().lower()
    if configured == "cli":
        return "cli"
    print()
    print("运行方式")
    print("-" * 40)
    print("  [1] 使用实例配置启动 QQ")
    print("  [2] 临时进入 CLI 聊天")
    choice = input("  请选择（默认 1）: ").strip() or "1"
    if choice == "2":
        return "cli"
    return "qq"


def _require_chat_provider_ready() -> None:
    from pupu.llm import ProviderConfigError, preflight_model_providers

    try:
        preflight_model_providers(require_chat=True)
    except ProviderConfigError as exc:
        print()
        print("[配置错误] 模型提供商还不能使用：")
        print(exc)
        print()
        print("请先打开 pupu.yaml，填写 llm.*.api_key。")
        sys.exit(2)


def _read_ws_port(inst: Path, default: int = 8081) -> int:
    env_path = inst / ".env.qq"
    if not env_path.is_file():
        return default
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PORT="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return int(raw)
    return default


def _start_cli() -> None:
    from pupu.cli import main

    main()


def _start_qq(config: dict, inst: Path) -> None:
    import nonebot

    qq_mode = str(config.get("qq_mode") or "napcat").strip().lower()
    env_file = inst / ".env.qq"

    if qq_mode == "napcat":
        nonebot.init(_env_file=str(env_file), driver="~fastapi")
        from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

        driver = nonebot.get_driver()
        driver.register_adapter(OneBotV11Adapter)
        port = _read_ws_port(inst)
        print("[仆仆QQ] 模式: NapCat (OneBot v11)")
        print()
        print("  仆仆已启动，正在等待 NapCat 连接...")
        print("  请确保 NapCat 已配置反向 WebSocket 地址:")
        print(f"    ws://127.0.0.1:{port}/onebot/v11/ws")
        print()

    elif qq_mode == "official":
        app_id = str(config.get("qq_app_id") or "").strip()
        app_secret = str(config.get("qq_app_secret") or "").strip()
        if not app_id or not app_secret:
            print("[错误] 当前实例缺少 qq_app_id / qq_app_secret，请先在控制台或 instance.json 中填写。")
            sys.exit(1)
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
        print(f"[错误] 当前实例 qq_mode={qq_mode!r}，不能作为 QQ bot 启动。")
        sys.exit(1)

    nonebot.load_plugins("plugins")
    nonebot.run()


def main() -> None:
    _load_global_config()
    instance_id = _select_instance_interactively()
    inst = _apply_instance_env(instance_id)
    setup_runtime_logging()
    cfg, _persona = instance_store.read_instance_files(instance_id)

    runtime_mode = _choose_runtime_mode(cfg)
    _require_chat_provider_ready()
    if runtime_mode == "cli":
        _start_cli()
    else:
        _start_qq(cfg, inst)


if __name__ == "__main__":
    main()
