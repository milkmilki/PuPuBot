import subprocess
import unittest
from pathlib import Path


TEXT_EXTENSIONS = {
    ".bat",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}

MOJIBAKE_MARKERS = [
    "\ufffd",
    "锛",
    "绗",
    "鐠",
    "鍥",
    "浜",
    "璁",
    "鎴",
    "浠",
    "妯",
    "鈫",
    "鈥",
    "鈹",
]


class TextEncodingIntegrityTests(unittest.TestCase):
    def test_tracked_text_files_are_utf8_without_common_mojibake(self):
        repo = Path(__file__).resolve().parents[1]
        tracked = subprocess.check_output(
            ["git", "ls-files"],
            cwd=repo,
            text=True,
            encoding="utf-8",
        ).splitlines()

        decode_failures: list[str] = []
        marker_hits: list[str] = []
        for relative in tracked:
            path = repo / relative
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                decode_failures.append(f"{relative}: {exc}")
                continue
            for marker in MOJIBAKE_MARKERS:
                if marker in text:
                    line_no = text[: text.index(marker)].count("\n") + 1
                    marker_hits.append(f"{relative}:{line_no}: {marker!r}")

        self.assertEqual(decode_failures, [])
        self.assertEqual(marker_hits, [])


if __name__ == "__main__":
    unittest.main()
