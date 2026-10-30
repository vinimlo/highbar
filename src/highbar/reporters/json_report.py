"""The report of a run as one JSON file (D19).

The file never misleads about the run that wrote it. It is replaced before the first
message, so a run that dies leaves "did not finish" and not an older green. It is
written at once, so nothing reads half of it. Its format is `report.schema.json`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from highbar.domain.models import RunSummary, Spec, iso_utc
from highbar.engine.secrets import redact

SCHEMA_VERSION = 1
GIT_TIMEOUT_S = 2


class ReportError(Exception):
    """The report file cannot be written."""


class JsonReport:
    """What a run has produced so far, and the file that says so."""

    def __init__(self, path: Path, *, version: str) -> None:
        self._path = path
        self._version = version
        self._started = time.time()
        self._git_sha = _git("rev-parse", "HEAD", cwd=Path.cwd())
        self._planned: dict[str, str] = {}  # spec path: sha256 before anything ran
        self._secrets: dict[str, str] = {}
        self._summaries: list[RunSummary] = []
        self._specs: list[dict[str, Any]] = []
        self._stopped: dict[str, str] | None = None

    def start(self) -> None:
        """Write the unfinished report. Raises ``ReportError``, before any message goes."""
        self._write(self._body(exit_code=None, complete=False))

    def plan(self, specs: list[tuple[str, Spec]]) -> None:
        """The specs that will run. Hashed now: a file can change during a long run."""
        for path, spec in specs:
            self._planned[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            self._secrets.update(spec.agent.secrets)

    def add(self, path: str, spec: Spec, summary: RunSummary, complete: bool) -> None:
        """One spec's result, final or as far as it got."""
        self._secrets.update(spec.agent.secrets)
        self._summaries.append(summary)
        self._specs.append(
            {
                "key": _spec_key(path),
                "path": path,
                "sha256": self._planned.get(path),
                "complete": complete,
                "summary": _totals([summary]),
                "warnings": list(summary.warnings),
                "tests": [asdict(test) for test in summary.tests],
            }
        )

    def stop(self, reason: str, message: str) -> None:
        """Why the run ended before all of its specs did."""
        self._stopped = {"reason": reason, "message": redact(message, self._secrets)}

    def finish(self, exit_code: int) -> None:
        """Write the final report. Raises ``ReportError``."""
        ran = {spec["path"] for spec in self._specs}
        complete = (
            self._stopped is None
            and ran >= set(self._planned)
            and all(spec["complete"] for spec in self._specs)
        )
        self._write(self._body(exit_code=exit_code, complete=complete))

    def _body(self, *, exit_code: int | None, complete: bool) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "highbar_version": self._version,
            "started_at": iso_utc(self._started),
            "duration_ms": (time.time() - self._started) * 1000,
            "exit_code": exit_code,
            "complete": complete,
            "stopped": self._stopped,
            "git_sha": self._git_sha,
            "summary": _totals(self._summaries),
            "specs": self._specs,
        }

    def _write(self, body: dict[str, Any]) -> None:
        # default=str: a value in agent.correlate may be a date from YAML. A surrogate the
        # agent sent has no UTF-8 form, and becomes the JSON escape it came from.
        text = json.dumps(_finite(body), ensure_ascii=False, indent=2, allow_nan=False, default=str)
        data = (text + "\n").encode("utf-8", "backslashreplace")
        temporary: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            mode = _file_mode(self._path)
            descriptor, temporary = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            with os.fdopen(descriptor, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self._path)
        except OSError as e:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            raise ReportError(f"cannot write the report to {self._path}: {e}") from None


def _totals(summaries: list[RunSummary]) -> dict[str, int]:
    return {
        "total": sum(s.total for s in summaries),
        "passed": sum(s.passed for s in summaries),
        "failed": sum(s.failed for s in summaries),
        "errored": sum(s.errored for s in summaries),
        "variations": sum(s.variations for s in summaries),
    }


def _finite(value: Any) -> Any:
    """``value`` with NaN and the infinities as null: JSON has no way to say them."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        mapping = cast("Mapping[Any, Any]", value)
        return {key: _finite(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[Any] | tuple[Any, ...]", value)
        return [_finite(item) for item in items]
    return value


def _file_mode(path: Path) -> int:
    """The mode the file already has, or what the umask gives a new one.

    ``mkstemp`` creates 0600, and the next step of a CI job may be another user.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def _git(*args: str, cwd: Path) -> str | None:
    """One line of git's output, or None where there is no git or no repository."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _spec_key(path: str) -> str:
    """What names a spec across reports: its path inside its repository (D15).

    The path as typed changes with where highbar runs. Outside a repository there is
    nothing better.
    """
    spec = Path(path).resolve()
    root = _git("rev-parse", "--show-toplevel", cwd=spec.parent)
    if root is None:
        return path
    try:
        return spec.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path
