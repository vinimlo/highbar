"""``highbar.run()``: run a spec from Python, the way ``highbar run`` does."""

from __future__ import annotations

import os

from rich.console import Console

from highbar.domain.models import RunSummary
from highbar.engine.parser import parse_spec
from highbar.engine.run_spec import run_spec
from highbar.variations.cache import VariationCache, find_cache_path
from highbar.variations.generator import VariationGenerator


def run(
    spec: str | os.PathLike[str],
    *,
    frozen: bool = False,
    keep_traces: bool = False,
) -> RunSummary:
    """Run one spec file and return its summary; prints the same report as the CLI.

    Raises ``SpecError`` for a spec that cannot run and ``AgentStartError`` for an
    agent that does not come up. The agent is stopped before this returns or raises.
    """
    path = os.fspath(spec)
    parsed = parse_spec(path)
    generator = VariationGenerator(VariationCache(find_cache_path()), frozen=frozen)
    return run_spec(path, parsed, generator=generator, keep_traces=keep_traces, console=Console())
