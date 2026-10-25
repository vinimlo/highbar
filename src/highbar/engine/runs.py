"""runs and min_pass (D9): how many times each phrasing runs, and how many must pass.

An LLM agent that does the right thing three times out of five passes a single run
three times out of five. Running it N times, with a minimum, turns that coin toss
into a gate.
"""

from __future__ import annotations

import math
from fractions import Fraction

from highbar.domain.models import Spec, Status, Test, TestResult
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


def phrasing_status(results: list[TestResult], min_pass: int) -> Status:
    """Passed when enough runs passed; an error, not a failure, when every run was one.

    A run with an error counts as not passed: it is not evidence the agent works.
    """
    if sum(1 for r in results if r.passed) >= min_pass:
        return Status.PASSED
    if all(r.status is Status.ERROR for r in results):
        return Status.ERROR
    return Status.FAILED
