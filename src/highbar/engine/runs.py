"""runs and min_pass (D9): how many times each phrasing runs, and how many must pass.

An LLM agent that does the right thing three times out of five passes a single run
three times out of five. Running it N times, with a minimum, turns that coin toss
into a gate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction

from highbar.domain.models import Spec, Status, Test
from highbar.engine.schema import SpecError


def resolve_runs(test: Test, default_runs: int) -> tuple[int, int]:
    """``(runs, min_pass)`` for ``test``.

    The test's own ``runs`` wins over ``default_runs`` (the CLI's ``--runs``, else the
    spec's ``defaults.runs``, else 1). ``min_pass`` defaults to every run; a fraction is
    rounded up, computed exactly so that 0.7 of 10 is 7, not 8.
    """
    runs = test.runs if test.runs is not None else default_runs
    if test.min_pass is None:
        return runs, runs
    if isinstance(test.min_pass, float):
        return runs, math.ceil(Fraction(str(test.min_pass)) * runs)
    return runs, test.min_pass


def check_runs(spec: Spec, default_runs: int) -> None:
    """Raise ``SpecError`` for a test whose minimum is more than its runs."""
    for i, suite in enumerate(spec.suites):
        for j, test in enumerate(suite.tests):
            runs, min_pass = resolve_runs(test, default_runs)
            if min_pass > runs:
                raise SpecError(
                    f"suites[{i}].tests[{j}]: min_pass {min_pass} is more than its {runs} runs"
                )


def phrasing_status(statuses: Sequence[Status], min_pass: int) -> Status:
    """Passed when enough runs passed; failed only when the failures alone prevent it.

    A run with an error counts as not passed: it is not evidence the agent works. Nor
    is it evidence it does wrong (D2): when the runs that could not be checked could
    have reached the minimum, the phrasing is unverified, not failed.
    """
    passed = sum(1 for status in statuses if status is Status.PASSED)
    errored = sum(1 for status in statuses if status is Status.ERROR)
    if passed >= min_pass:
        return Status.PASSED
    if passed + errored >= min_pass:
        return Status.ERROR
    return Status.FAILED
