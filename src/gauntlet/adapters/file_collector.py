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
        """Return the highest seq number in the trace file, or -1 if empty."""
        if not self._path.exists():
            return -1

        last_seq = -1
        for line in self._read_lines():
            data = json.loads(line)
            last_seq = max(last_seq, data["seq"])

        return last_seq

    def collect(self, since: int) -> list[Trace]:
        """Read all traces with seq > since, returned as domain Trace objects."""
        if not self._path.exists():
            return []

        traces: list[Trace] = []
        for line in self._read_lines():
            data = json.loads(line)
            if data["seq"] > since:
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
