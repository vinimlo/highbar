"""Trace writer — appends one record of the trace contract (v1) per line.

Standard library only: this runs inside the agent, often in production images.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

CONTRACT_VERSION = 1

logger = logging.getLogger("highbar")


class TraceWriter:
    """Appends records to a JSONL file, one ``os.write`` per line.

    A single write to a file opened with ``O_APPEND`` keeps lines from different
    threads and processes (uvicorn workers) from interleaving. The file is opened on
    every write because its path comes from the environment and may change.

    Writing never raises: a trace that cannot be written must not break the agent's
    conversation. It is logged, and the run's proof of observation turns the loss
    into an error instead of a green.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def write(self, path: str, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(
                {"v": CONTRACT_VERSION, **record}, default=_as_text, ensure_ascii=False
            )
            data = (line + "\n").encode("utf-8")
            with self._lock:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
        except Exception as e:
            # Serializing runs arbitrary __str__/__repr__ (a detached ORM row raises in
            # it), and a value can nest too deep: none of that may reach the agent.
            logger.warning("highbar: could not write a trace record to %s: %r", path, e)


def _as_text(value: object) -> str:
    """JSON fallback for values it does not know: their text, or a placeholder."""
    try:
        return str(value)
    except Exception:
        return f"<unserializable {type(value).__name__}>"
