"""Exit 1 unless the release tag is "v" + the version in pyproject.toml.

The release workflow runs it first: a tag pushed by mistake, or a version bump
forgotten, must not reach PyPI, where a version can never be uploaded twice.

    python scripts/check_release_tag.py "$GITHUB_REF_NAME"
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def version(pyproject: Path) -> str:
    return str(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"])


def main(argv: list[str], pyproject: Path = PYPROJECT) -> int:
    if len(argv) != 1:
        print("usage: check_release_tag.py <tag>", file=sys.stderr)
        return 2
    tag, expected = argv[0], f"v{version(pyproject)}"
    if tag != expected:
        print(
            f"tag {tag} is not the package version: pyproject.toml says {expected}", file=sys.stderr
        )
        return 1
    print(f"tag {tag} is the package version")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
