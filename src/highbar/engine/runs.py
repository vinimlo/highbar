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


def resolve_runs(
    test: Test, default_runs: int, default_min_pass: int | float | None = None
) -> tuple[int, int]:
    """``(runs, min_pass)`` for ``test``.

    The test's own ``runs`` wins over ``default_runs`` (the CLI's ``--runs``, else the
    spec's ``defaults.runs``, else 1). The test's own ``min_pass`` wins over
    ``default_min_pass`` (the spec's ``defaults.min_pass``), else every run must pass.
    A fraction is of the test's runs, rounded up, computed exactly so that 0.7 of 10 is
    7, not 8; a whole number stays the same whatever the runs.
    """
    runs = test.runs if test.runs is not None else default_runs
    min_pass = test.min_pass if test.min_pass is not None else default_min_pass
    if min_pass is None:
        return runs, runs
    if isinstance(min_pass, float):
        return runs, math.ceil(Fraction(str(min_pass)) * runs)
    return runs, min_pass


def check_runs(spec: Spec, default_runs: int) -> None:
    """Raise ``SpecError`` for a test whose minimum is more than its runs.

    The minimum is the one the runner uses: the test's, else ``spec.default_min_pass``.
    """
    for i, suite in enumerate(spec.suites):
        for j, test in enumerate(suite.tests):
            runs, min_pass = resolve_runs(test, default_runs, spec.default_min_pass)
            if min_pass <= runs:
                continue
            where = f"suites[{i}].tests[{j}]"
            if test.min_pass is None:
                raise SpecError(
                    f"{where}: min_pass {min_pass} from defaults.min_pass is more than its "
                    f"{runs} runs; give the test its own min_pass, or make defaults.min_pass "
                    "a fraction"
                )
            raise SpecError(f"{where}: min_pass {min_pass} is more than its {runs} runs")


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
