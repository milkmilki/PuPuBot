import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from pupu.instance_context import InstanceContext, activate_instance_context_global

TEST_TMP_ROOT = Path(__file__).resolve().parent / "_tmp" / "runtime"


@contextlib.contextmanager
def simulate_narrow_console(encoding: str = "gbk"):
    """Simulate a console whose encoding cannot represent astral-plane chars.

    Patches both the raising print and ``sys.stdout.encoding`` so the fallback
    escape path in ``logging_utils`` runs regardless of the host's real stdout
    encoding (UTF-8 on macOS/Linux/CI, GBK on some Windows consoles). Yields the
    list of lines that reached the console.
    """
    import pupu.logging_utils as logging_utils

    console_lines: list[str] = []

    def narrow_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(arg) for arg in args) + end
        text.encode(encoding)
        console_lines.append(text)

    class _NarrowStdout:
        def __init__(self, enc: str):
            self.encoding = enc

        def write(self, *_args, **_kwargs):
            return 0

        def flush(self):
            pass

    with patch.object(logging_utils, "_original_print", side_effect=narrow_print):
        with patch.object(sys, "stdout", _NarrowStdout(encoding)):
            yield console_lines


def ensure_test_tmp_root() -> Path:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(TEST_TMP_ROOT)
    return TEST_TMP_ROOT


def make_test_temp_dir(prefix: str = "case-") -> tempfile.TemporaryDirectory:
    ensure_test_tmp_root()
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(TEST_TMP_ROOT))


def cleanup_test_tmp_root() -> None:
    root = ensure_test_tmp_root()
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def activate_test_instance(
    db_path: Path,
    *,
    display_name: str = "Test PuPu",
    instance_id: str | None = None,
    fresh: bool = False,
) -> InstanceContext:
    """Activate a real InstanceContext for tests that need storage paths."""
    ensure_test_tmp_root()
    db_path = Path(db_path).resolve()
    root = TEST_TMP_ROOT / "pupu_test_instances"
    instance_dir = root / (instance_id or db_path.stem)
    if fresh and instance_dir.exists():
        shutil.rmtree(instance_dir, ignore_errors=True)
    data_dir = instance_dir / "data"
    db_path = data_dir / db_path.name
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance.json").write_text(
        json.dumps({"display_name": display_name, "qq_mode": "cli"}, ensure_ascii=False),
        encoding="utf-8",
    )
    persona_path = instance_dir / "persona.json"
    if not persona_path.exists():
        persona_path.write_text(
            json.dumps({"name": display_name}, ensure_ascii=False),
            encoding="utf-8",
        )
    ctx = InstanceContext.from_instance_dir(instance_dir)
    activate_instance_context_global(ctx)
    return ctx
