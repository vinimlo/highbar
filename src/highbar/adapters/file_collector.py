"""Trace collector — reads the trace contract (JSONL) into domain models.

The file is read by byte offset: a checkpoint is the position after the last complete
line, and a collect reads from there. Re-reading the whole file for every test made
each test slower than the last on a long run.

Rules of the contract (v1) that live here:

- Unknown fields and record types are ignored, so emitters can add to the format.
- A line that is not valid JSON is an error that names its line. Skipping it would
  hide a tool call, and the test that needed it would pass or fail for nothing. The
  other lines of the read are still read, and come with the error: the usage in them
  was spent, and the late records of earlier tests still count for them.
- A line without ``v`` is v0, the format Gauntlet and early emitters wrote: a tool
  call whose extra top-level fields are its context.
- Only lines that end in a newline are read. A writer may be in the middle of one.
- A turn's ``usage`` is optional, and what cannot be read in it counts as missing: a
  writer that had its own ``usage`` field keeps working, and a budget on a value that
  was dropped ends in ``error``, never in a pass. A ``usage`` with nothing readable is
  remembered as such: alone it says nothing, but next to a readable one it was spent too.

With ``trace_format="otlp"`` each line is OpenTelemetry's OTLP/JSON instead, mapped in
``highbar.adapters.otlp`` (HB-48). The reading by offset and a bad line are the same.
"""

from __future__ import annotations

import glob
import heapq
import json
import math
import os
import stat
from collections.abc import Collection
from pathlib import Path
from typing import Any

from highbar._narrow import as_dict, as_list
from highbar.adapters.otlp import OtlpLineError, add_otlp
from highbar.domain.models import (
    Hello,
    LlmCall,
    Retrieval,
    RetrievedItem,
    Trace,
    TurnRecord,
    Usage,
    Window,
)
from highbar.engine.observation import AgentNotObservedError

CONTRACT_VERSION = 1

# Fields a v0 line has of its own; any other top-level field is correlation context.
_V0_FIELDS = frozenset(
    {"v", "id", "seq", "fn", "args", "result", "started_at", "ended_at", "error"}
)

_BACKWARD_CHUNK = 64 * 1024

# Before the first `hello` of a file, no `bye` can end what it declared.
_NO_HELLO = object()


class TraceFormatError(ValueError):
    """A trace line that cannot be read. The message is ``<file> line <n>: <problem>``.

    ``window`` holds the records of every line of the read that could be read, or is
    None when the collector read none of them.
    """

    def __init__(self, message: str, window: Window | None = None) -> None:
        super().__init__(message)
        self.window = window


class _BadLine(Exception):
    """A problem with one line, before its line number is known."""


class FileTraceCollector:
    """Reads trace records written to a JSONL file, in ``trace_format``."""

    def __init__(self, path: str | Path, *, trace_format: str = "highbar") -> None:
        self._path = Path(path)
        self._otlp = trace_format == "otlp"

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
        except OSError as e:
            raise _unreadable(self._path, e) from None

    def collect(self, since: int) -> Window:
        """The records in the complete lines written after byte ``since``.

        A file smaller than ``since`` was recreated (a restarted agent, a rotated
        file), so it is read from the start. Raises ``TraceFormatError`` for the first
        line that cannot be read, with the records of all the others.
        """
        try:
            with open(self._path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                start = since if since <= size else 0
                f.seek(start)
                data = f.read()
        except FileNotFoundError:
            return Window()
        except OSError as e:
            raise _unreadable(self._path, e) from None

        complete = data[: data.rfind(b"\n") + 1]
        window = Window(end=start + len(complete))
        offset = start
        problem: str | None = None
        for raw in complete.splitlines(keepends=True):
            try:
                if self._otlp:
                    _add_otlp(window, raw)
                else:
                    _add(window, raw)
            except _BadLine as e:
                if problem is None:
                    problem = f"{self._path} line {self._line_at(offset)}: {e}"
            offset += len(raw)
        if problem is not None:
            raise TraceFormatError(problem, window)
        return window

    def declared(self) -> frozenset[str] | None:
        """The tools the agent declared since its last `hello`, or None if none (spec D4).

        Read once at the start of a run, from the whole file: an agent declares its
        tools when it starts, before the first test's cursor. Declarations before the
        last `hello` are an earlier process's, whose tools may since have changed. A
        `bye` with the pid of the last `hello` says that process ended, so what it
        declared says nothing about the code running now: None (review of #52). Only
        `hello`, `tools` and `bye` lines are parsed, one at a time, and a line that
        cannot be read is skipped: it is history, which no test reads. OTLP declares
        nothing (HB-48).
        """
        if self._otlp:
            return None
        current: set[str] | None = None
        hello_pid: Any = _NO_HELLO
        ended = False
        try:
            with open(self._path, "rb") as f:
                for raw in f:
                    if b'"hello"' not in raw and b'"tools"' not in raw and b'"bye"' not in raw:
                        continue
                    try:
                        record = as_dict(json.loads(raw))
                    except ValueError:
                        continue
                    kind = record.get("type") if record is not None else None
                    if record is None or kind not in ("hello", "tools", "bye"):
                        continue
                    if kind == "bye":
                        # Another process's end (a forked child's) ends nothing here.
                        ended = ended or record.get("pid") == hello_pid
                        continue
                    if kind == "hello":
                        current, hello_pid, ended = None, record.get("pid"), False
                        if "tools" not in record:
                            continue
                    try:
                        names = _names(record, "tools" if kind == "hello" else "names")
                    except _BadLine:
                        continue
                    current = set(names) if current is None else current | set(names)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise _unreadable(self._path, e) from None
        return frozenset(current) if current is not None and not ended else None

    def _line_at(self, offset: int) -> int:
        """The 1-based number of the line that starts at byte ``offset``."""
        with open(self._path, "rb") as f:
            return f.read(offset).count(b"\n") + 1


def is_glob(path: str | Path) -> bool:
    """Whether ``agent.traces`` names several files: a ``*``, ``?`` or ``[`` in it (HB-15)."""
    return any(char in "*?[" for char in str(path))


def trace_collector(
    path: str | Path, *, trace_format: str = "highbar"
) -> FileTraceCollector | GlobTraceCollector:
    """The collector for ``agent.traces``: one file, or every file a glob matches.

    A path that names a file is that file, a ``[`` in its name or not, as before HB-15.
    """
    if is_glob(path) and not Path(path).is_file():
        return GlobTraceCollector(path, trace_format=trace_format)
    return FileTraceCollector(path, trace_format=trace_format)


class GlobTraceCollector:
    """Reads the trace files a glob matches, one per process of the agent, as one (HB-15).

    A server and its queue workers that share one file depend on appends that do not
    interleave, which a bind mount on macOS does not promise. With ``{pid}`` in
    ``HIGHBAR_TRACES_URL`` each process writes its own file, and this reads them all.

    Each file is read by byte offset, as ``FileTraceCollector`` reads one. A checkpoint
    is a number that stands for the offset of every file at that moment, and a file the
    glob starts to match later is read from its start. The records of a read are put in
    time order across files (a tool by when it ended, a turn and a hello by ``ts``), and
    each file keeps its own order. A directory the glob matches is not a trace file, and
    a link is another name for a file it matches: each file is read once.
    """

    def __init__(self, pattern: str | Path, *, trace_format: str = "highbar") -> None:
        self._pattern = str(pattern)
        self._format = trace_format
        # Checkpoint 0 is every file from its start; each later one, the offsets it holds.
        self._marks: list[dict[Path, int]] = [{}]
        # Every name a checkpoint has held an offset for: the name a file is read by.
        self._known: set[Path] = set()

    @property
    def path(self) -> Path:
        return Path(self._pattern)

    def missing(self) -> bool:
        """No file matches: a wrong pattern, or no process of the agent has written yet."""
        return not self._files()

    def checkpoint(self) -> int:
        files = self._files(self._known)
        return self._mark({file: self._reader(file).checkpoint() for file in files})

    def collect(self, since: int) -> Window:
        """The records written in every file after checkpoint ``since``, in time order.

        Raises ``TraceFormatError`` for the first line that cannot be read, with the
        records of all the others, as one file does.
        """
        offsets = self._marks[since]
        windows: list[Window] = []
        ends: dict[Path, int] = {}
        problem: TraceFormatError | None = None
        for file in self._files(self._known):
            reader = self._reader(file)
            try:
                window = reader.collect(since=offsets.get(file, 0))
            except TraceFormatError as e:
                problem = problem or e
                window = e.window if e.window is not None else Window(end=reader.checkpoint())
            windows.append(window)
            ends[file] = window.end
        merged = _merged(windows, end=self._mark(ends))
        if problem is not None:
            raise TraceFormatError(str(problem), merged)
        return merged

    def declared(self) -> frozenset[str] | None:
        """The tools every process declared since its own last `hello`, together."""
        found = [self._reader(file).declared() for file in self._files()]
        declared = [names for names in found if names is not None]
        return frozenset[str]().union(*declared) if declared else None

    def _reader(self, file: Path) -> FileTraceCollector:
        return FileTraceCollector(file, trace_format=self._format)

    def _files(self, known: Collection[Path] = ()) -> list[Path]:
        """Each file the glob matches, once, by one of its names (review of #52).

        A link, or a second hard link, names a file already matched: read under both
        names, each record would count twice. The name the run already reads it by is
        kept, so a link made during the run does not read the file again from its start;
        else the name that is not a link, else the first.
        """
        names: dict[tuple[int, int], Path] = {}
        for path in sorted(map(Path, glob.glob(self._pattern))):
            try:
                status = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(status.st_mode):
                continue
            identity = (status.st_dev, status.st_ino)
            kept = names.get(identity)
            if kept is None or _rank(path, known) < _rank(kept, known):
                names[identity] = path
        return sorted(names.values())

    def _mark(self, offsets: dict[Path, int]) -> int:
        self._marks.append(offsets)
        self._known.update(offsets)
        return len(self._marks) - 1


def _rank(path: Path, known: Collection[Path]) -> tuple[bool, bool]:
    """Which of two names of one file to read it by: lower first."""
    return (path not in known, path.is_symlink())


def _merged(windows: list[Window], *, end: int) -> Window:
    """The records of several files' windows in time order, each file's order kept."""
    return Window(
        tools=list(heapq.merge(*(w.tools for w in windows), key=lambda tool: tool.ended_at)),
        turns=list(heapq.merge(*(w.turns for w in windows), key=lambda turn: turn.ts)),
        hellos=list(heapq.merge(*(w.hellos for w in windows), key=lambda hello: hello.ts)),
        declarations=[names for w in windows for names in w.declarations],
        end=end,
    )


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


def _unreadable(path: Path, error: OSError) -> AgentNotObservedError:
    """A trace path that is there but cannot be read: named, not a traceback (HB-15).

    Docker makes a directory when a bind mount names a file that does not exist yet:
    the most common way to get here, so it gets its own message.
    """
    where = path.absolute()
    if isinstance(error, IsADirectoryError) or path.is_dir():
        return AgentNotObservedError(
            f"agent.traces {where} is a directory, not a file, so no test can see what the "
            "agent does. Docker makes one when a bind mount names a file that does not exist "
            "yet: remove it, create the file and start the agent again"
        )
    return AgentNotObservedError(
        f"could not read agent.traces {where}: {error.strerror or error}, so no test can "
        "see what the agent does"
    )


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


def _object(raw: bytes) -> dict[str, Any]:
    """The JSON object of one line, or ``_BadLine``."""
    try:
        parsed: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        reason = e.msg if isinstance(e, json.JSONDecodeError) else "not UTF-8"
        raise _BadLine(f"not valid JSON ({reason})") from None
    record = as_dict(parsed)
    if record is None:
        raise _BadLine(f"not a JSON object, got {type(parsed).__name__}")
    return record


def _add_otlp(window: Window, raw: bytes) -> None:
    """The spans of one OTLP line, added whole or, if the line cannot be read, not at all."""
    if not raw.strip():
        return
    try:
        add_otlp(window, _object(raw))
    except OtlpLineError as e:
        raise _BadLine(str(e)) from None


def _add(window: Window, raw: bytes) -> None:
    """The record of one line, added whole or, if the line cannot be read, not at all."""
    if not raw.strip():
        return
    record = _object(raw)

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
        raw_usage = record.get("usage")
        usage = _usage(raw_usage)
        window.turns.append(
            TurnRecord(
                ts=_number(record, "ts", 0.0),
                context=_context(record),
                message=_text(record.get("message")),
                response=_text(record.get("response")),
                usage=usage,
                # An empty one says nothing. Anything else said something highbar cannot read.
                usage_unreadable=usage is None and bool(raw_usage),
            )
        )
    elif kind == "hello":
        pid = record.get("pid")
        names = _names(record, "tools") if "tools" in record else None
        window.hellos.append(
            Hello(
                ts=_number(record, "ts", 0.0),
                sdk=_text(record.get("sdk")),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            )
        )
        if names is not None:
            window.declarations.append(names)
    elif kind == "tools":
        window.declarations.append(_names(record, "names"))
    elif kind == "llm":
        window.llms.append(_llm(record))
    elif kind == "retrieval":
        window.retrievals.append(_retrieval(record))
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
        has_result="result" in record,
        started_at=_number(record, "started_at", ts),
        ended_at=_number(record, "ended_at", ts),
        error=error,
        context=context,
    )


def _usage(raw: Any) -> Usage | None:
    """A turn's ``usage``, or None when it has nothing that can be read."""
    fields = as_dict(raw)
    if fields is None:
        return None
    usage = Usage(
        model=_label(fields.get("model")),
        provider=_label(fields.get("provider")),
        input_tokens=_count(fields.get("input_tokens")),
        output_tokens=_count(fields.get("output_tokens")),
        cached_input_tokens=_count(fields.get("cached_input_tokens")),
        cost=_cost(fields.get("cost")),
        currency=_currency(fields.get("currency")),
        llm_calls=_count(fields.get("llm_calls")),
    )
    return None if usage == Usage() else usage


def _label(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _currency(value: Any) -> str | None:
    """Upper case: `usd` and `USD` are one currency, and must add up as one."""
    label = _label(value)
    return label.upper() if label is not None else None


def _count(value: Any) -> int | None:
    """A whole number of at least 0. Some writers serialize every number as a float."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost(value: Any) -> float | None:
    """A finite number of at least 0: Python's JSON reads a bare NaN as a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too long for a float
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _names(record: dict[str, Any], key: str) -> tuple[str, ...]:
    """The tool names a declaration lists. A malformed one would hide what it declares."""
    names = as_list(record.get(key))
    if names is None or not all(isinstance(name, str) for name in names):
        raise _BadLine(f"'{key}' must be a list of tool names")
    return tuple(str(name) for name in names)


def _llm(record: dict[str, Any]) -> LlmCall:
    """One call to the model (HB-11). Only ``roles`` is strict: a check reads its order."""
    raw_roles = record.get("roles")
    roles: tuple[str, ...] | None = None
    if raw_roles is not None:
        items = as_list(raw_roles)
        if items is None or not all(isinstance(role, str) for role in items):
            raise _BadLine(f"'roles' must be a list of role names, got {raw_roles!r}")
        roles = tuple(items)
    error = record.get("error")
    latency = record.get("latency_ms")
    read = record.get("read")
    if read is not None and not isinstance(read, bool):
        # Read loosely, "no" would vouch for a count nobody read.
        raise _BadLine(f"'read' must be true or false, got {read!r}")
    return LlmCall(
        ts=_number(record, "ts", 0.0),
        context=_context(record),
        model=_text(record.get("model")),
        provider=_text(record.get("provider")),
        attempt=_whole(record.get("attempt")),
        fallback_from=_text(record.get("fallback_from")),
        roles=roles,
        input_tokens=_whole(record.get("input_tokens")),
        output_tokens=_whole(record.get("output_tokens")),
        cache_tokens=_whole(record.get("cache_tokens")),
        latency_ms=_number(record, "latency_ms", 0.0) if latency is not None else None,
        error=str(error) if error else None,  # false, "" and null all say "no error"
        source=_text(record.get("source")),
        read=read,
        unread=_text(record.get("unread")),
        cost=_cost(record.get("cost")),
        currency=_currency(record.get("currency")),
        call=_text(record.get("call")),
    )


def _retrieval(record: dict[str, Any]) -> Retrieval:
    """What entered the prompt (HB-3). The items are strict: every check reads them."""
    raw_items = record.get("items", [])
    items = as_list(raw_items)
    if items is None or not all(as_dict(item) is not None for item in items):
        raise _BadLine(f"'items' must be a list of objects, got {raw_items!r}")
    read: list[RetrievedItem] = []
    for n, raw in enumerate(items):
        item = as_dict(raw) or {}
        if item.get("id") is None and item.get("title") is None:
            raise _BadLine(f"items[{n}] has neither 'id' nor 'title': no check can name it")
        raw_chunks = item.get("chunks", [])
        chunks = as_list(raw_chunks)
        if chunks is None or not all(_whole(chunk) is not None for chunk in chunks):
            raise _BadLine(f"items[{n}].chunks must be a list of whole numbers, got {raw_chunks!r}")
        read.append(
            RetrievedItem(
                id=_text(item.get("id")),
                title=_text(item.get("title")),
                source=_text(item.get("source")),
                chunks=tuple(int(chunk) for chunk in chunks),
                tokens=_whole(item.get("tokens")),
            )
        )
    expired = as_list(record.get("expired")) or []
    return Retrieval(
        ts=_number(record, "ts", 0.0),
        context=_context(record),
        query=_text(record.get("query")),
        search_query=_text(record.get("search_query")),
        strategy=_text(record.get("strategy")),
        skipped_reason=_text(record.get("skipped_reason")),
        items=tuple(read),
        expired=tuple(entry for entry in (as_dict(e) for e in expired) if entry is not None),
        tokens=_whole(record.get("tokens")),
    )


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
