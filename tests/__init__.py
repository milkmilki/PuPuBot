"""Shared unittest bootstrap for PuPuBot tests."""

from __future__ import annotations

import tempfile
from pathlib import Path


TEST_TMP_ROOT = Path(__file__).resolve().parent / "_tmp" / "runtime"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Keep all raw tempfile.TemporaryDirectory() calls inside the repo test scratch
# area, which avoids Windows profile/sandbox temp directory differences.
tempfile.tempdir = str(TEST_TMP_ROOT)
