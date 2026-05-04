"""Runtime logging helpers for mirroring prints and stderr into a log file."""

import atexit
import builtins
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

_initialized = False
_log_file = None
_log_path = None
_print_lock = threading.Lock()
_original_print = builtins.print
_original_stderr = sys.stderr


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


def _get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_log_dir() -> Path:
    inst = os.environ.get("PUPU_INSTANCE_DIR")
    if inst:
        log_dir = Path(inst) / "data" / "logs"
    else:
        log_dir = _get_project_root() / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _build_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d")
    return _ensure_log_dir() / f"pupu-{stamp}.log"


def _ensure_current_log_file():
    global _log_file, _log_path
    if not _initialized:
        return _log_file

    target_path = _build_log_path()
    with _print_lock:
        if _log_file is not None and _log_path == target_path:
            return _log_file

        old_file = _log_file
        old_path = _log_path
        _log_path = target_path
        _log_file = _log_path.open("a", encoding="utf-8", buffering=1)
        if old_file is not None:
            try:
                old_file.flush()
                old_file.close()
            except Exception:
                pass
        if old_path is not None and old_path != _log_path:
            _log_file.write(f"[pupu] logging rotated to {_log_path}\n")
            _log_file.flush()
        return _log_file


def _get_sink():
    return _ensure_current_log_file()


def _patched_print(*args, **kwargs):
    file_target = kwargs.get("file")
    _original_print(*args, **kwargs)

    if file_target not in (None, sys.stdout, sys.stderr, _original_stderr):
        return

    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    text = sep.join(str(arg) for arg in args) + end
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


def setup_runtime_logging() -> str:
    global _initialized, _log_file, _log_path
    if _initialized:
        return str(get_log_file_path())

    _log_path = _build_log_path()
    _log_file = _log_path.open("a", encoding="utf-8", buffering=1)
    builtins.print = _patched_print
    sys.stderr = _TeeStderr(_original_stderr, _get_sink)

    def _close_log_file():
        global _log_file
        if _log_file is not None:
            try:
                _log_file.flush()
                _log_file.close()
            finally:
                _log_file = None

    atexit.register(_close_log_file)
    _initialized = True
    print(f"[pupu] logging to {_log_path}")
    return str(_log_path)
