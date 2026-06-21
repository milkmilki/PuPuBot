import json
import tempfile
from pathlib import Path

from pupu.instance_context import InstanceContext, activate_instance_context_global


def activate_test_instance(
    db_path: Path,
    *,
    display_name: str = "Test PuPu",
    instance_id: str | None = None,
) -> InstanceContext:
    """Activate a real InstanceContext for tests that need storage paths."""
    db_path = Path(db_path).resolve()
    root = Path(tempfile.gettempdir()) / "pupu_test_instances"
    instance_dir = root / (instance_id or db_path.stem)
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
