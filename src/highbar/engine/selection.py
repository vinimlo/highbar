"""Part of the tests of a run: by suite (``--suite``) and by name (``-k``) (HB-17).

The specs are parsed whole, so a typo anywhere still stops the run, and then cut down
to the tests picked. A test keeps its object, and so what it measures and its
``definition_sha256``. A spec with no test left is left out: its agent never starts.
"""

from __future__ import annotations

import difflib
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, replace

from highbar.domain.models import Spec, Suite, Test, stable_key


class SelectionError(ValueError):
    """The options pick nothing: the run would check nothing, so it does not start."""


@dataclass(frozen=True)
class Selection:
    """What the options picked, for the report and the run's last line.

    A run of part of the tests can exit 0: only this, and ``complete: false`` in the
    report, say that the rest did not run. With ``left_out`` 0 the options cut nothing,
    and the report still says they were given.
    """

    suites: tuple[str, ...]
    keywords: tuple[str, ...]
    picked: int  # tests, not executions
    left_out: int

    def line(self, *, not_run: int = 0) -> str | None:
        """The run's last line, or ``None`` when the options picked every test.

        ``left_out`` counts only what the options cut. ``not_run`` is how many of the
        tests picked a stop (``--max-cost``, ``--max-errors``) did not let run: said
        apart, since the stop's own message says why.
        """
        if not self.left_out:
            return None
        options = _options(self.suites, self.keywords)
        picked, total = self.picked, self.picked + self.left_out
        line = (
            f"part of the tests: {picked} of {total} picked by {options}, {self.left_out} left out"
        )
        if not_run:
            line += f"; of the {picked} picked, {not_run} did not run: the run stopped"
        return line


def selection_of(
    before: list[tuple[str, Spec]],
    after: list[tuple[str, Spec]],
    *,
    suites: Sequence[str],
    keywords: Sequence[str],
) -> Selection | None:
    """What ``select`` kept of ``before`` in ``after``; ``None`` when no option was given."""
    if not suites and not keywords:
        return None
    picked = _count(after)
    return Selection(tuple(suites), tuple(keywords), picked, _count(before) - picked)


def _count(specs: list[tuple[str, Spec]]) -> int:
    return sum(len(suite.tests) for _, spec in specs for suite in spec.suites)


def _options(suites: Sequence[str], keywords: Sequence[str]) -> str:
    """The options as a shell reads them: ``-k 'cancel it'`` is one value, and pastes back."""
    return " ".join(
        [
            *(f"--suite {shlex.quote(name)}" for name in suites),
            *(f"-k {shlex.quote(word)}" for word in keywords),
        ]
    )


def select(
    specs: list[tuple[str, Spec]], *, suites: Sequence[str], keywords: Sequence[str]
) -> list[tuple[str, Spec]]:
    """The specs with only the tests that ``suites`` and ``keywords`` pick.

    ``suites`` are names, exactly as written. ``keywords`` are pieces of a test's
    ``<suite>/<name>`` or of its ``id``, ignoring case: one is enough. Given both, a
    test must be in one of the suites and match one of the keywords.
    """
    if not suites and not keywords:
        return specs
    _check_suites_exist(specs, suites)
    picked: list[tuple[str, Spec]] = []
    for path, spec in specs:
        kept = [
            replace(suite, tests=tests)
            for suite in spec.suites
            if not suites or suite.name in suites
            if (tests := [test for test in suite.tests if _matches(suite, test, keywords)])
        ]
        if kept:
            picked.append((path, replace(spec, suites=kept)))
    if not picked:
        options = _options(suites, keywords)
        raise SelectionError(f"no test matches {options}: nothing would be checked")
    return picked


def _matches(suite: Suite, test: Test, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    names = {stable_key(suite.name, test).lower(), f"{suite.name}/{test.plain_name}".lower()}
    return any(word.lower() in name for word in keywords for name in names)


def _check_suites_exist(specs: list[tuple[str, Spec]], suites: Sequence[str]) -> None:
    """A suite name no spec has is a typo, not a selection of nothing."""
    known = sorted({suite.name for _, spec in specs for suite in spec.suites})
    for name in suites:
        if name in known:
            continue
        close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        hint = f" — did you mean '{close[0]}'?" if close else ""
        raise SelectionError(
            f"--suite '{name}': no spec has a suite with that name{hint} "
            f"(suites: {', '.join(known)})"
        )
