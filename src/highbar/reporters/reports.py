"""Several formats of one run's report (HB-19), each in its own file.

One report keeps the run, as a single ``--report`` does, and every write goes to each
file in its own format: they all say "did not finish" from the start and the same
result at the end, so no two of them can tell a different story about one run.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from highbar.reporters.json_report import JsonReport, ReportError


class Reports(JsonReport):
    """One run's report, written in each of ``parts``' formats to each one's file."""

    def __init__(self, parts: list[JsonReport], *, version: str) -> None:
        super().__init__(parts[0].path, version=version)
        self._parts = parts

    @property
    def paths(self) -> list[Path]:
        return [part.path for part in self._parts]

    def _write(self, body: dict[str, Any]) -> None:
        """Every file gets ``body``, or none keeps it.

        A file that cannot be written makes the run exit 2, so no other file may be left
        saying how the run ended. Each one is written next to its place first, and they
        are put in place only once all are written. A replace that still fails (the
        file became a folder) puts the ones already replaced back to "did not finish".
        """
        staged: list[tuple[JsonReport, str]] = []
        try:
            for part in self._parts:
                staged.append((part, part._stage(body)))
        except ReportError:
            for _, temporary in staged:
                Path(temporary).unlink(missing_ok=True)
            raise
        for done, (part, temporary) in enumerate(staged):
            try:
                part._replace(temporary)
            except ReportError:
                for _, waiting in staged[done + 1 :]:
                    Path(waiting).unlink(missing_ok=True)
                self._put_back([replaced for replaced, _ in staged[:done]])
                raise

    def _put_back(self, parts: list[JsonReport]) -> None:
        """These files say the run did not finish, as far as they can be written."""
        unfinished = self._body(exit_code=None, complete=False)
        for part in parts:
            with suppress(ReportError):
                part._write(unfinished)
