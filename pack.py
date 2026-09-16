"""One-click packing script — creates a deployable zip of pupu."""

import os
import zipfile
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

INCLUDE = [
    "pupu/",
    "pupu_console/",
    "plugins/",
    "docs/",
    "start.py",
    "requirements.txt",
    "pupu.yaml.example",
    ".gitignore",
    "README.md",
    "deploy.bat",
    "启动仆仆.bat",
    "启动仆仆控制台.bat",
    "pack.py",
]

DATA_FILES = []

EXCLUDE_PATTERNS = (
    "__pycache__",
    ".pyc",
    ".pyo",
    ".db",
    ".log",
    ".bak",
    "data/",
    "instances/",
    ".env",
    ".env.qq",
    "pupu.yaml",
    "config.json",
)


def should_include(path: str) -> bool:
    for pat in EXCLUDE_PATTERNS:
        if pat in path:
            return False
    return True


def pack():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"pupu_{timestamp}.zip"
    zip_path = os.path.join(PROJECT_DIR, zip_name)

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in INCLUDE:
            full = os.path.join(PROJECT_DIR, entry)
            if os.path.isdir(full):
                for root, dirs, files in os.walk(full):
                    for f in files:
                        abs_path = os.path.join(root, f)
                        rel_path = os.path.relpath(abs_path, PROJECT_DIR)
                        if should_include(rel_path):
                            zf.write(abs_path, rel_path)
                            count += 1
            elif os.path.isfile(full):
                zf.write(full, entry)
                count += 1
            else:
                print(f"  [skip] {entry} (not found)")

        for df in DATA_FILES:
            full = os.path.join(PROJECT_DIR, df)
            if os.path.isfile(full):
                zf.write(full, df)
                count += 1
                db_size = os.path.getsize(full)
                print(f"  [data] {df} ({db_size / 1024:.0f} KB)")
            else:
                print(f"  [skip] {df} (no data yet)")

    print()
    print(f"Done! {count} files -> {zip_name} ({os.path.getsize(zip_path) / 1024:.0f} KB)")
    print()
    print("To deploy on another machine:")
    print("  1. Unzip")
    print("  2. Run deploy.bat (Windows) or: python -m venv ForFun && ForFun/Scripts/pip install -r requirements.txt")
    print("  3. Copy pupu.yaml.example to pupu.yaml and edit provider / QQ / NapCat settings")
    print("  4. Run: 启动仆仆.bat or ForFun\\Scripts\\python.exe start.py")
    print("  5. The launcher will ask you to create/select an instance")


if __name__ == "__main__":
    print("=" * 40)
    print("  Packing pupu...")
    print("=" * 40)
    print()
    pack()
