"""The quickstart `highbar init` writes: examples/quickstart, packaged in the wheel."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

QUICKSTART_FILES = ("agent.py", "specs/quickstart.highbar.yaml")


def quickstart_files() -> dict[str, bytes]:
    """Path relative to the project → content, for each file `highbar init` writes."""
    source = _source()
    contents: dict[str, bytes] = {}
    for name in QUICKSTART_FILES:
        node = source
        for part in name.split("/"):
            node = node / part
        contents[name] = node.read_bytes()
    return contents


def _source() -> Traversable:
    packaged = files("highbar") / "_quickstart"
    if packaged.is_dir():
        return packaged
    # Running from a checkout: the example is what the wheel packages.
    return Path(__file__).resolve().parents[3] / "examples" / "quickstart"
