"""Runtime logging helpers for mirroring prints and stderr into a log file."""

import atexit
import builtins
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from .app_config import get_repo_root
from .instance_context import get_current_instance_context

_initialized = False
_log_file = None
_log_path = None
_log_files: dict[Path, object] = {}
_print_lock = threading.Lock()
_original_print = builtins.print
_original_stderr = sys.stderr
LOG_RETENTION_DAYS = 3

_VERBOSE_CONSOLE_PREFIXES = (
    "[pupu][semantic]",
    "[pupu][tool]",
    "[pupu] batch review",
    "[pupu] dialogue decision",
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def is_debug_console_enabled() -> bool:
    return _truthy(os.environ.get("PUPU_DEBUG_CONSOLE"))


def set_debug_console_enabled(enabled: bool) -> None:
    os.environ["PUPU_DEBUG_CONSOLE"] = "1" if enabled else "0"


def _is_verbose_console_line(text: str) -> bool:
    stripped = str(text or "").lstrip()
    return any(stripped.startswith(prefix) for prefix in _VERBOSE_CONSOLE_PREFIXES)


class _TeeStderr:
    def __init__(self, original, sink_getter):
        self._original = original
        self._sink_getter = sink_getter
        self.encoding = getattr(original, "encoding", "utf-8")

    def write(self, data):
        text = str(data)
        self._original.write(text)
        sink = self._sink_getter()
        if sink is not None and text:
            with _print_lock:
                sink.write(text)
                sink.flush()
        return len(text)

    def flush(self):
        self._original.flush()
        sink = self._sink_getter()
        if sink is not None:
            with _print_lock:
                sink.flush()

    def isatty(self):
        return bool(getattr(self._original, "isatty", lambda: False)())

    def fileno(self):
        return self._original.fileno()


def _ensure_log_dir() -> Path:
    ctx = get_current_instance_context()
    if ctx is not None:
        log_dir = ctx.logs_dir
    else:
        log_dir = get_repo_root() / "instances" / "_shared" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _log_sort_key(path: Path) -> tuple[str, str]:
    name = path.name
    date_text = name[5:-4] if name.startswith("pupu-") and name.endswith(".log") else ""
    if len(date_text) == 8 and date_text.isdigit():
        return date_text, name
    return "", name


def _daily_log_paths(log_dir: Path | None = None) -> list[Path]:
    root = log_dir or _ensure_log_dir()
    out: list[Path] = []
    for path in root.glob("pupu-*.log"):
        name = path.name
        date_text = name[5:-4] if name.startswith("pupu-") and name.endswith(".log") else ""
        if len(date_text) == 8 and date_text.isdigit():
            out.append(path)
    return out


def prune_old_logs(keep: int = LOG_RETENTION_DAYS, log_dir: Path | None = None) -> list[Path]:
    """Delete older daily PuPu log files, keeping the newest snapshots."""
    try:
        keep_count = max(1, int(keep))
    except Exception:
        keep_count = LOG_RETENTION_DAYS

    logs = sorted(_daily_log_paths(log_dir), key=_log_sort_key, reverse=True)
    current_path = _log_path.resolve() if _log_path is not None else None
    deleted: list[Path] = []
    for path in logs[keep_count:]:
        try:
            if current_path is not None and path.resolve() == current_path:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(path)
    return deleted


def _build_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d")
    return _ensure_log_dir() / f"pupu-{stamp}.log"


def _ensure_current_log_file():
    global _log_file, _log_path
    if not _initialized:
        return _log_file

    target_path = _build_log_path()
    with _print_lock:
        existing = _log_files.get(target_path)
        if existing is not None and not getattr(existing, "closed", False):
            _log_path = target_path
            _log_file = existing
            return existing

        old_path = _log_path
        for path, file in list(_log_files.items()):
            if path.parent == target_path.parent and path != target_path:
                try:
                    file.flush()
                    file.close()
                except Exception:
                    pass
                _log_files.pop(path, None)
        _log_path = target_path
        _log_file = _log_path.open("a", encoding="utf-8", buffering=1)
        _log_files[target_path] = _log_file
        if old_path is not None and old_path != _log_path:
            _log_file.write(f"[pupu] logging rotated to {_log_path}\n")
            _log_file.flush()
        prune_old_logs(log_dir=_log_path.parent)
        return _log_file


def _get_sink():
    return _ensure_current_log_file()


def _patched_print(*args, **kwargs):
    file_target = kwargs.get("file")
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    text = sep.join(str(arg) for arg in args) + end

    should_echo = True
    if file_target in (None, sys.stdout, sys.stderr, _original_stderr):
        should_echo = is_debug_console_enabled() or not _is_verbose_console_line(text)

    if should_echo:
        _original_print(*args, **kwargs)

    if file_target not in (None, sys.stdout, sys.stderr, _original_stderr):
        return

    sink = _get_sink()
    if sink is None or not text:
        return

    with _print_lock:
        sink.write(text)
        if kwargs.get("flush", False) or text.endswith("\n"):
            sink.flush()


def get_log_file_path() -> str | None:
    if _initialized:
        _ensure_current_log_file()
    return str(_log_path) if _log_path is not None else None


def close_current_instance_log_sinks() -> None:
    """Close cached log files for the active instance context, if any."""
    global _log_file, _log_path
    ctx = get_current_instance_context()
    if ctx is None:
        return
    target_dir = ctx.logs_dir.resolve()
    with _print_lock:
        for path, file in list(_log_files.items()):
            try:
                same_dir = path.parent.resolve() == target_dir
            except Exception:
                same_dir = False
            if not same_dir:
                continue
            try:
                file.flush()
                file.close()
            except Exception:
                pass
            _log_files.pop(path, None)
            if _log_path is not None and path == _log_path:
                _log_path = None
                _log_file = None


def close_all_log_sinks() -> None:
    """Close every cached runtime log sink."""
    global _log_file, _log_path
    with _print_lock:
        for path, file in list(_log_files.items()):
            try:
                file.flush()
                file.close()
            except Exception:
                pass
            _log_files.pop(path, None)
        _log_file = None
        _log_path = None


def setup_runtime_logging() -> str:
    global _initialized, _log_file, _log_path
    if _initialized:
        return str(get_log_file_path())

    _log_path = _build_log_path()
    _log_file = _log_path.open("a", encoding="utf-8", buffering=1)
    _log_files[_log_path] = _log_file
    prune_old_logs(log_dir=_log_path.parent)
    builtins.print = _patched_print
    sys.stderr = _TeeStderr(_original_stderr, _get_sink)

    def _close_log_file():
        close_all_log_sinks()

    atexit.register(_close_log_file)
    _initialized = True
    print(f"[pupu] logging to {_log_path}")
    return str(_log_path)
