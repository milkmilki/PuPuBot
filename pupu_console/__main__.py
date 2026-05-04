"""Run the Web console: ``python -m pupu_console``."""

from __future__ import annotations

import os
import webbrowser

import uvicorn


def main() -> None:
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
