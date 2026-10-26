"""Trace collector — reads the trace contract (JSONL) into domain models.

The file is read by byte offset: a checkpoint is the position after the last complete
line, and a collect reads from there. Re-reading the whole file for every test made
each test slower than the last on a long run.

Rules of the contract (v1) that live here:

- Unknown fields and record types are ignored, so emitters can add to the format.
- A line that is not valid JSON is an error that names its line. Skipping it would
  hide a tool call, and the test that needed it would pass or fail for nothing.
- A line without ``v`` is v0, the format Gauntlet and early emitters wrote: a tool
  call whose extra top-level fields are its context.
- Only lines that end in a newline are read. A writer may be in the middle of one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from highbar._narrow import as_dict, as_list
from highbar.domain.models import Hello, Trace, TurnRecord, Window

CONTRACT_VERSION = 1

# Fields a v0 line has of its own; any other top-level field is correlation context.
_V0_FIELDS = frozenset(
    {"v", "id", "seq", "fn", "args", "result", "started_at", "ended_at", "error"}
)

_BACKWARD_CHUNK = 64 * 1024


class TraceFormatError(ValueError):
    """A trace line that cannot be read. The message is ``<file> line <n>: <problem>``."""


class _BadLine(Exception):
    """A problem with one line, before its line number is known."""


class FileTraceCollector:
    """Reads trace records written to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def missing(self) -> bool:
        """The file does not exist: a wrong path, or an agent that has not written yet."""
        return not self._path.exists()

    def checkpoint(self) -> int:
        """The byte offset just after the last complete line (0 if there is no file)."""
        try:
            with open(self._path, "rb") as f:
                return _end_of_last_line(f)
        except FileNotFoundError:
            return 0

    def collect(self, since: int) -> Window:
        """The records in the complete lines written after byte ``since``.

        A file smaller than ``since`` was recreated (a restarted agent, a rotated
        file), so it is read from the start. Raises ``TraceFormatError``.
        """
        try:
            with open(self._path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                start = since if since <= size else 0
                f.seek(start)
                data = f.read()
        except FileNotFoundError:
            return Window()

        complete = data[: data.rfind(b"\n") + 1]
        window = Window(end=start + len(complete))
        offset = start
        for raw in complete.splitlines(keepends=True):
            try:
                _add(window, raw)
            except _BadLine as e:
                raise TraceFormatError(f"{self._path} line {self._line_at(offset)}: {e}") from None
            offset += len(raw)
        return window

    def declared(self) -> frozenset[str] | None:
        """The tools the agent declared since its last `hello`, or None if none (spec D4).

        Read once at the start of a run, from the whole file: an agent declares its
        tools when it starts, before the first test's cursor. Declarations before the
        last `hello` are an earlier process's, whose tools may since have changed. Only
        `hello` and `tools` lines are parsed, one at a time, and a line that cannot be
        read is skipped: it is history, which no test reads.
        """
        current: set[str] | None = None
        try:
            with open(self._path, "rb") as f:
                for raw in f:
                    if b'"hello"' not in raw and b'"tools"' not in raw:
                        continue
                    try:
                        record = as_dict(json.loads(raw))
                    except ValueError:
                        continue
                    kind = record.get("type") if record is not None else None
                    if record is None or kind not in ("hello", "tools"):
                        continue
                    if kind == "hello":
                        current = None
                        if "tools" not in record:
                            continue
                    try:
                        names = _names(record, "tools" if kind == "hello" else "names")
                    except _BadLine:
                        continue
                    current = set(names) if current is None else current | set(names)
        except FileNotFoundError:
            return None
        return frozenset(current) if current is not None else None

    def _line_at(self, offset: int) -> int:
        """The 1-based number of the line that starts at byte ``offset``."""
        with open(self._path, "rb") as f:
            return f.read(offset).count(b"\n") + 1


class NoTraceCollector:
    """For an attached agent whose spec checks no tool calls, and names no trace file."""

    path = Path("(no trace file)")

    def checkpoint(self) -> int:
        return 0

    def collect(self, since: int) -> Window:
        return Window()

    def declared(self) -> frozenset[str] | None:
        return None

    def missing(self) -> bool:
        return False


def _end_of_last_line(f: Any) -> int:
    """Offset after the last newline of an open binary file, reading it backwards."""
    position = f.seek(0, os.SEEK_END)
    while position > 0:
        step = min(_BACKWARD_CHUNK, position)
        position -= step
        f.seek(position)
        found = f.read(step).rfind(b"\n")
        if found != -1:
            return position + found + 1
    return 0


def _add(window: Window, raw: bytes) -> None:
    if not raw.strip():
        return
    try:
        parsed: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        reason = e.msg if isinstance(e, json.JSONDecodeError) else "not UTF-8"
        raise _BadLine(f"not valid JSON ({reason})") from None
    record = as_dict(parsed)
    if record is None:
        raise _BadLine(f"not a JSON object, got {type(parsed).__name__}")

    version = record.get("v")
    if version is None or (not isinstance(version, bool) and version == 0):
        window.tools.append(_tool(record, context=_v0_context(record)))
        return
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _BadLine(f"unknown trace format version {version!r}")
    if version > CONTRACT_VERSION:
        raise _BadLine(
            f"trace format v{version} is newer than this highbar, which reads "
            f"v{CONTRACT_VERSION}; upgrade highbar"
        )

    kind = record.get("type")
    if kind == "tool":
        window.tools.append(_tool(record, context=_context(record)))
    elif kind == "turn":
        window.turns.append(
            TurnRecord(
                ts=_number(record, "ts", 0.0),
                context=_context(record),
                message=_text(record.get("message")),
                response=_text(record.get("response")),
            )
        )
    elif kind == "hello":
        pid = record.get("pid")
        window.hellos.append(
            Hello(
                ts=_number(record, "ts", 0.0),
                sdk=_text(record.get("sdk")),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            )
        )
        if "tools" in record:
            window.declarations.append(_names(record, "tools"))
    elif kind == "tools":
        window.declarations.append(_names(record, "names"))
    # Any other type is from a newer emitter: ignored, as the contract says.


def _tool(record: dict[str, Any], *, context: dict[str, Any]) -> Trace:
    fn = record.get("fn")
    if not isinstance(fn, str) or not fn:
        raise _BadLine("tool record without 'fn'")
    raw_args = record.get("args", {})
    args = as_dict(raw_args)
    if args is None:
        raise _BadLine(f"'args' must be an object, got {type(raw_args).__name__}")
    ts = _number(record, "ts", 0.0)
    error = record.get("error")
    if not error:
        error = None  # false, "" and null all say "no error"
    elif not isinstance(error, str):
        error = json.dumps(error, ensure_ascii=False)
    return Trace(
        fn=fn,
        args=args,
        result=record.get("result"),
        started_at=_number(record, "started_at", ts),
        ended_at=_number(record, "ended_at", ts),
        error=error,
        context=context,
    )


def _names(record: dict[str, Any], key: str) -> tuple[str, ...]:
    """The tool names a declaration lists. A malformed one would hide what it declares."""
    names = as_list(record.get(key))
    if names is None or not all(isinstance(name, str) for name in names):
        raise _BadLine(f"'{key}' must be a list of tool names")
    return tuple(str(name) for name in names)


def _context(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("context", {})
    context = as_dict(raw)
    if context is None:
        raise _BadLine(f"'context' must be an object, got {type(raw).__name__}")
    return context


def _v0_context(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in _V0_FIELDS}


def _number(record: dict[str, Any], key: str, default: float) -> float:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
