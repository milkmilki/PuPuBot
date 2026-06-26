import json
import shutil
import tempfile
from pathlib import Path

from pupu.instance_context import InstanceContext, activate_instance_context_global

TEST_TMP_ROOT = Path(__file__).resolve().parent / "_tmp" / "runtime"


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
