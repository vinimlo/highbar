"""Several formats of one run's report (HB-19), each in its own file.

One report keeps the run, as a single ``--report`` does, and every write goes to each
file in its own format: they all say "did not finish" from the start and the same
result at the end, so no two of them can tell a different story about one run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from highbar.reporters.json_report import JsonReport


class Reports(JsonReport):
    """One run's report, written in each of ``parts``' formats to each one's file."""

    def __init__(self, parts: list[JsonReport], *, version: str) -> None:
        super().__init__(parts[0].path, version=version)
        self._parts = parts

    @property
    def paths(self) -> list[Path]:
        return [part.path for part in self._parts]

    def _write(self, body: dict[str, Any]) -> None:
        for part in self._parts:
            part._write(body)
