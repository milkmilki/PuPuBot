"""Run the Web console: ``python -m pupu_console``."""

from __future__ import annotations

import os
import webbrowser

import uvicorn

from pupu.app_config import apply_app_config_env, ensure_app_config_file


def main() -> None:
    cfg_path, created = ensure_app_config_file()
    if created:
        print(f"[pupu] created default config: {cfg_path}")
        print("[pupu] fill llm.*.api_key before starting an instance.")
    apply_app_config_env()
    host = os.environ.get("PUPU_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("PUPU_CONSOLE_PORT", "8770"))
    url = f"http://{host}:{port}/"
    webbrowser.open(url)
    uvicorn.run(
        "pupu_console.server:app",
        host=host,
        port=port,
        factory=False,
        reload=False,
    )


if __name__ == "__main__":
    main()
