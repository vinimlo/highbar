"""Print the first line of a README translation: which English README it translates.

    python scripts/readme_source.py

The line names README.md by a hash of its text and by the last commit that changed it.
tests/docs/test_translations.py compares the hash with README.md as it is now, so a
translation made from an older English README fails until it is brought up to date.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def readme_hash(root: Path = ROOT) -> str:
    return hashlib.sha256((root / "README.md").read_bytes()).hexdigest()[:12]


def source_line(root: Path = ROOT) -> str:
    commit = subprocess.run(
        ["git", "log", "-1", "--format=%h", "--", "README.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return f"<!-- source: README.md sha256={readme_hash(root)} commit={commit or 'uncommitted'} -->"


if __name__ == "__main__":
    sys.stdout.write(source_line() + "\n")
