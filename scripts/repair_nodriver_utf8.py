"""Repair upstream nodriver CDP source files with non-UTF8 bytes after pip install.

The project's Dockerfile already performs the same normalization for Python 3.14.
Bot Hosting uses the Python runtime instead of the Dockerfile, so apply it there.
Only malformed UTF-8 .py files are touched; there is no network access here.
"""
from pathlib import Path
import sys


def normalize() -> int:
    repaired = 0
    seen = set()
    for folder in sys.path:
        if not folder:
            continue
        candidate = Path(folder) / "nodriver" / "cdp"
        if not candidate.is_dir() or candidate in seen:
            continue
        seen.add(candidate)
        for path in candidate.glob("*.py"):
            raw = path.read_bytes()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                path.write_text(raw.decode("latin-1"), encoding="utf-8")
                repaired += 1
    return repaired


if __name__ == "__main__":
    print("nodriver_utf8_repaired=" + str(normalize()), flush=True)
