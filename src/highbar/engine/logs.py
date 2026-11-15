"""Where a run's logs go: ``.highbar/logs/``, relative to where highbar runs."""

from __future__ import annotations

import time
from pathlib import Path


def log_path(spec_path: str, kind: str = "") -> Path:
    """``.highbar/logs/<spec stem>[-<kind>]-<timestamp>.log``, never an existing file.

    Never an existing file: two specs with the same name in the same second would
    share a log, and one's failure would show the other's output.
    """
    stem = Path(spec_path).name.removesuffix(".yaml").removesuffix(".highbar")
    name = f"{stem}-{kind}" if kind else stem
    base = Path(".highbar") / "logs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    path, n = base.with_suffix(".log"), 1
    while path.exists():
        n += 1
        path = base.parent / f"{base.name}-{n}.log"
    return path
