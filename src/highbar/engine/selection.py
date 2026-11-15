"""Part of the tests of a run: by suite (``--suite``) and by name (``-k``) (HB-17).

The specs are parsed whole, so a typo anywhere still stops the run, and then cut down
to the tests picked. A test keeps its object, and so what it measures and its
``definition_sha256``. A spec with no test left is left out: its agent never starts.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import replace

from highbar.domain.models import Spec, Suite, Test, stable_key


class SelectionError(ValueError):
    """The options pick nothing: the run would check nothing, so it does not start."""


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
        options = [*(f"--suite {name}" for name in suites), *(f"-k {word}" for word in keywords)]
        raise SelectionError(f"no test matches {' '.join(options)}: nothing would be checked")
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
