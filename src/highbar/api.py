"""``highbar.run()``: run a spec from Python, the way ``highbar run`` does."""

from __future__ import annotations

import os

from rich.console import Console

from highbar.domain.models import RunSummary
from highbar.domain.protocols import Reporter
from highbar.engine.parser import parse_spec
from highbar.engine.run_spec import load_session_hooks, run_spec
from highbar.engine.runs import check_runs
from highbar.engine.schema import SpecError
from highbar.variations.cache import VariationCache, find_cache_path
from highbar.variations.generator import VariationGenerator


def run(
    spec: str | os.PathLike[str],
    *,
    frozen: bool = False,
    keep_traces: bool = False,
    runs: int | None = None,
    reporter: Reporter | None = None,
) -> RunSummary:
    """Run one spec file and return its result; prints the same report as the CLI.

    ``runs`` is the CLI's ``--runs``: it takes the place of the spec's
    ``defaults.runs``, and a test's own ``runs`` still wins. ``reporter`` receives each
    test as it ends, in place of the terminal output.

    The summary holds every test in its final state, in ``tests``.

    Raises ``SpecError`` for a spec that cannot run, ``SessionError`` for session hooks
    that cannot be loaded, and ``AgentStartError`` for an agent that does not come up.
    An agent highbar started is stopped before this returns or raises.
    """
    path = os.fspath(spec)
    parsed = parse_spec(path)
    # Checked at run time too: callers from Python may pass anything.
    if runs is not None and (isinstance(runs, bool) or not isinstance(runs, int) or runs < 1):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"runs must be a whole number of at least 1, got {runs!r}")
    try:
        check_runs(parsed, parsed.default_runs if runs is None else runs)
    except SpecError as e:
        raise SpecError(f"{path}: {e}") from None
    generator = VariationGenerator(VariationCache(find_cache_path()), frozen=frozen)
    return run_spec(
        path,
        parsed,
        generator=generator,
        keep_traces=keep_traces,
        console=Console(),
        session=load_session_hooks(path, parsed),
        default_runs=runs,
        reporter=reporter,
    )
