"""Unified entry point for pupu — CLI chat or QQ bot."""

import sys

from pupu.config import load_config, save_config
from pupu.logging_utils import setup_runtime_logging

setup_runtime_logging()


def select_mode():
    config = load_config()
    print("=" * 40)
    print("  仆仆启动器")
    print("=" * 40)
    print()
    print("  [1] 终端聊天")
    print("  [2] QQ 机器人 - NapCat")
    print("  [3] QQ 机器人 - 官方")
    print()

    choice = input("  请选择: ").strip()

    if choice == "1":
        return "cli", config
    elif choice == "2":
        config["qq_mode"] = "napcat"
        save_config(config)
        return "qq", config
    elif choice == "3":
        config["qq_mode"] = "official"
        if not config.get("qq_app_id") or not config.get("qq_app_secret"):
            print()
            app_id = input("  AppID: ").strip()
            app_secret = input("  AppSecret: ").strip()
            if not app_id or not app_secret:
                print("[错误] AppID 和 AppSecret 不能为空")
                sys.exit(1)
            config["qq_app_id"] = app_id
            config["qq_app_secret"] = app_secret
        save_config(config)
        return "qq", config
    else:
        print(f"[错误] 无效选择: {choice}")
        sys.exit(1)


def start_cli():
    from pupu.cli import main
    main()


def start_qq(config):
    import nonebot

    qq_mode = config["qq_mode"]

    if qq_mode == "napcat":
        nonebot.init(_env_file=".env.qq", driver="~fastapi")
        from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
        driver = nonebot.get_driver()
        driver.register_adapter(OneBotV11Adapter)
        print("[仆仆QQ] 模式: NapCat (OneBot v11)")
        print()
        print("  仆仆已启动，正在等待 NapCat 连接...")
        print("  请确保 NapCat 已配置反向 WebSocket 地址:")
        print("    ws://127.0.0.1:8081/onebot/v11/ws")
        print()
        print("  如果还没启动 NapCat，现在启动它。")
        print("  连接成功后会显示提示。")
        print()

    elif qq_mode == "official":
        app_id = config.get("qq_app_id", "")
        app_secret = config.get("qq_app_secret", "")
        nonebot.init(
            _env_file=".env.qq",
            driver="~httpx+~websockets",
            qq_bots=[{
                "id": app_id,
                "token": "",
                "secret": app_secret,
                "intent": {
                    "c2c_group_at_messages": True,
                },
            }],
        )
        from nonebot.adapters.qq import Adapter as QQAdapter
        driver = nonebot.get_driver()
        driver.register_adapter(QQAdapter)
        print(f"[仆仆QQ] 模式: QQ 官方机器人 (AppID: {app_id})")

    nonebot.load_plugins("plugins")
    nonebot.run()


if __name__ == "__main__":
    mode, config = select_mode()
    if mode == "cli":
        start_cli()
    else:
        start_qq(config)
