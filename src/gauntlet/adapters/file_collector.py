"""File-based trace collector — reads JSONL trace files into domain models."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.domain.models import Trace


class FileTraceCollector:
    """Reads trace boxes from a JSONL file."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def checkpoint(self) -> int:
        """Return the current line count, or 0 if the file doesn't exist."""
        if not self._path.exists():
            return 0

        return len(self._read_lines())

    def collect(self, since: int) -> list[Trace]:
        """Read traces written after the checkpoint (skipping first `since` lines)."""
        if not self._path.exists():
            return []

        lines = self._read_lines()
        traces: list[Trace] = []
        for line in lines[since:]:
            data = json.loads(line)
            traces.append(
                Trace(
                    id=data["id"],
                    seq=data["seq"],
                    fn=data["fn"],
                    args=data["args"],
                    result=data["result"],
                    started_at=data["started_at"],
                    ended_at=data["ended_at"],
                    error=data.get("error"),
                )
            )

        return traces

    def _read_lines(self) -> list[str]:
        text = self._path.read_text().strip()
        if not text:
            return []
        return text.split("\n")
