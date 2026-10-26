"""highbar's trace contract v1, written without the highbar package.

Copy it, or write the same lines in any language: one JSON object per line,
appended to the file named by HIGHBAR_TRACES_URL. Unset, nothing is written.
"""

import json
import os
import time


def _write(record: dict, test: str | None = None) -> None:
    path = os.environ.get("HIGHBAR_TRACES_URL")
    if not path:
        return
    line = {"v": 1, "ts": time.time(), **record}
    if test:  # the X-Highbar-Test header of the request being handled
        line["context"] = {"test": test}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:  # one write per line, so lines from two threads never interleave
        os.write(fd, (json.dumps(line) + "\n").encode())
    finally:
        os.close(fd)


def hello(tools: list[str]) -> None:
    """When the agent starts, with the tools it records: highbar waits for it."""
    _write({"type": "hello", "sdk": "reference-emitter/1", "pid": os.getpid(), "tools": tools})


def tool(fn, args, *, result=None, error=None, started_at, test=None) -> None:
    """One tool call. `error` says why it failed, also when it returned the failure."""
    record = {"fn": fn, "args": args, "result": result, "error": error}
    _write({"type": "tool", **record, "started_at": started_at, "ended_at": time.time()}, test)


def turn(test: str | None) -> None:
    """One message handled: before the reply goes out, also when the handling failed."""
    _write({"type": "turn"}, test)
