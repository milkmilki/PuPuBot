"""Pre-commit branch scope guard for PuPuBot.

The hook is intentionally conservative:
- ``main`` is the backend/runtime source of truth and must not receive Siri UI
  files.
- ``siri`` is the desktop shell branch and must not receive backend/runtime
  files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


SIRI_ALLOWED_PREFIXES = (
    "desktop/pupu-siri/",
    "scripts/show-pupu-siri.ps1",
    "启动仆仆Siri.bat",
)
SIRI_BACKEND_PREFIXES = (
    "pupu/",
    "pupu_console/",
    "tests/",
    "docs/",
)
SIRI_BACKEND_FILES = (
    "CHANGELOG.md",
    "README.md",
    "pupu.yaml.example",
    "requirements.txt",
)
MAIN_FORBIDDEN_PREFIXES = (
    "desktop/pupu-siri/",
    "scripts/show-pupu-siri.ps1",
    "启动仆仆Siri.bat",
)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def _current_branch() -> str:
    try:
        return _git("branch", "--show-current")
    except subprocess.CalledProcessError:
        return ""


def _staged_paths() -> list[str]:
    try:
        raw = _git("diff", "--cached", "--name-only", "-z")
    except subprocess.CalledProcessError:
        return []
    return [part.replace("\\", "/") for part in raw.split("\0") if part]


def _is_merge_commit() -> bool:
    try:
        git_dir = _git("rev-parse", "--git-dir")
    except subprocess.CalledProcessError:
        return False
    from pathlib import Path

    return (Path(git_dir) / "MERGE_HEAD").exists()


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = str(PurePosixPath(path))
    for pattern in patterns:
        if pattern.endswith("/"):
            if normalized.startswith(pattern):
                return True
        elif normalized == pattern:
            return True
    return False


def _blocked_on_main(paths: list[str]) -> list[str]:
    return [path for path in paths if _matches(path, MAIN_FORBIDDEN_PREFIXES)]


def _blocked_on_siri(paths: list[str]) -> list[str]:
    blocked = []
    for path in paths:
        if _matches(path, SIRI_ALLOWED_PREFIXES):
            continue
        if _matches(path, SIRI_BACKEND_PREFIXES) or path in SIRI_BACKEND_FILES:
            blocked.append(path)
    return blocked


def _print_block(branch: str, blocked: list[str]) -> None:
    print(f"[branch-scope] Refusing commit on {branch!r}; staged files cross branch scope.")
    print("")
    for path in blocked[:30]:
        print(f"  - {path}")
    if len(blocked) > 30:
        print(f"  ... and {len(blocked) - 30} more")
    print("")
    if branch == "main":
        print("main is for backend/runtime work. Commit Siri UI files on branch 'siri'.")
    elif branch == "siri":
        print("siri is for desktop UI files. Commit backend/runtime changes on 'main' first.")
    print("Override only when you are intentionally doing branch surgery:")
    print("  git commit --no-verify")


def main() -> int:
    branch = _current_branch()
    paths = _staged_paths()
    if not branch or not paths:
        return 0

    if branch == "siri" and _is_merge_commit():
        return 0

    if branch == "main":
        blocked = _blocked_on_main(paths)
    elif branch == "siri":
        blocked = _blocked_on_siri(paths)
    else:
        blocked = []

    if blocked:
        _print_block(branch, blocked)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
