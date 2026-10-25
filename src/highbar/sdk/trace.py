"""Trace writer — writes atomic boxes to JSONL file."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid


class TraceWriter:
    """Writes tool invocation traces to a JSONL file.

    Each trace is an atomic "box" with a unique fingerprint ID
    and auto-incrementing sequence number.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._seq = 0
        self._lock = threading.Lock()

    def write(
        self,
        fn: str,
        args: dict[str, object],
        result: object,
        started_at: float | None = None,
        ended_at: float | None = None,
        error: str | None = None,
    ) -> str:
        """Write a trace box. Returns the fingerprint ID."""
        now = time.time()
        if started_at is None:
            started_at = now
        if ended_at is None:
            ended_at = now

        nonce = uuid.uuid4().hex
        fingerprint = self._make_fingerprint(fn, args, nonce)

        with self._lock:
            seq = self._seq
            self._seq += 1

            trace = {
                "id": fingerprint,
                "seq": seq,
                "fn": fn,
                "args": args,
                "result": result,
                "started_at": started_at,
                "ended_at": ended_at,
            }

            if error is not None:
                trace["error"] = error

            line = json.dumps(trace, default=str) + "\n"
            with open(self._path, "a") as f:
                f.write(line)

        return fingerprint

    @staticmethod
    def _make_fingerprint(fn: str, args: dict[str, object], nonce: str) -> str:
        payload = json.dumps({"fn": fn, "args": args, "nonce": nonce}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
