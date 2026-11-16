"""What examples/prompt-loop/loop.py decides, from reports written here.

The end-to-end cycles are in tests/integration/test_prompt_loop.py; these are the rules
that no example run reaches, such as a run that --max-cost stopped.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

LOOP = Path(__file__).resolve().parents[2] / "examples" / "prompt-loop" / "loop.py"


@pytest.fixture(scope="module")
def loop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prompt_loop", LOOP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["prompt_loop"] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def _report(
    tmp_path: Path,
    name: str,
    statuses: dict[str, str],
    *,
    exit_code: int = 0,
    cost: dict[str, float] | None = None,
    complete: bool = True,
) -> Path:
    tests = [
        {"id": test_id, "name": test_id.split("/")[-1], "status": status}
        for test_id, status in statuses.items()
    ]
    totals: dict[str, Any] | None = None
    if cost is not None:
        totals = {"cost": cost, "complete": True}
    body = {
        "exit_code": exit_code,
        "complete": complete,
        "totals": totals,
        "specs": [{"key": "specs/a.highbar.yaml", "tests": tests}],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _side(loop: ModuleType, tmp_path: Path, label: str, *rounds: tuple[dict[str, str], int]) -> Any:
    runs = [
        loop.read_run(_report(tmp_path, f"{label}-{i}", statuses, exit_code=code), code)
        for i, (statuses, code) in enumerate(rounds, start=1)
    ]
    return loop.Side(label, Path(f"{label}.md"), runs, len(rounds))


BASE = {"s/a": "passed", "s/b": "failed", "s/c": "failed"}
ONE_FIXED = {"s/a": "passed", "s/b": "passed", "s/c": "failed"}
TWO_FIXED = {"s/a": "passed", "s/b": "passed", "s/c": "passed"}


def _clean(loop: ModuleType) -> Any:
    return loop.Comparison(exit_code=0, lines=[], worse=[], better=["s/b"], report=Path("c.json"))


def test_a_candidate_that_passes_more_with_no_regression_is_adopted(
    loop: ModuleType, tmp_path: Path
) -> None:
    base = _side(loop, tmp_path, "base", (BASE, 1), (BASE, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", (ONE_FIXED, 1), (ONE_FIXED, 1), (ONE_FIXED, 1))

    adoptable, reason = loop.judge(base, head, _clean(loop))

    assert adoptable, reason
    assert "2 of 3 tests passed, the baseline 1" in reason


def test_a_run_stopped_by_max_cost_is_not_adoptable(loop: ModuleType, tmp_path: Path) -> None:
    base = _side(loop, tmp_path, "base", (BASE, 1), (BASE, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", (TWO_FIXED, 0), (TWO_FIXED, 3), (TWO_FIXED, 0))

    adoptable, reason = loop.judge(base, head, _clean(loop))

    assert not adoptable
    assert reason == "round 2 was stopped by --max-cost (exit 3)"


def test_a_run_that_could_not_run_is_not_adoptable(loop: ModuleType, tmp_path: Path) -> None:
    base = _side(loop, tmp_path, "base", (BASE, 1), (BASE, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", ({}, 2), (TWO_FIXED, 0), (TWO_FIXED, 0))

    adoptable, reason = loop.judge(base, head, _clean(loop))

    assert not adoptable
    assert reason == "round 1 could not run (exit 2)"


def test_an_error_on_the_baseline_blocks_every_candidate(loop: ModuleType, tmp_path: Path) -> None:
    errored = {**BASE, "s/c": "error"}
    base = _side(loop, tmp_path, "base", (BASE, 1), (errored, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", (TWO_FIXED, 0), (TWO_FIXED, 0), (TWO_FIXED, 0))

    adoptable, reason = loop.judge(base, head, _clean(loop))

    assert not adoptable
    assert reason == "the baseline cannot be judged: s/c ended in error in round 2"


def test_a_test_that_passes_in_only_some_rounds_does_not_count(
    loop: ModuleType, tmp_path: Path
) -> None:
    base = _side(loop, tmp_path, "base", (BASE, 1), (BASE, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", (ONE_FIXED, 1), (BASE, 1), (ONE_FIXED, 1))

    adoptable, reason = loop.judge(base, head, _clean(loop))

    assert not adoptable
    assert reason == "1 of 3 tests passed, not more than the baseline's 1"


def test_a_regression_rejects_even_with_more_passes(loop: ModuleType, tmp_path: Path) -> None:
    base = _side(loop, tmp_path, "base", (BASE, 1), (BASE, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", (TWO_FIXED, 0), (TWO_FIXED, 0), (TWO_FIXED, 0))
    worse = loop.Comparison(exit_code=1, lines=[], worse=["s/a"], better=[], report=Path("c"))

    adoptable, reason = loop.judge(base, head, worse)

    assert not adoptable
    assert reason == "highbar compare found a regression: s/a"


def test_a_comparison_that_could_not_run_is_not_adoptable(loop: ModuleType, tmp_path: Path) -> None:
    base = _side(loop, tmp_path, "base", (BASE, 1), (BASE, 1), (BASE, 1))
    head = _side(loop, tmp_path, "head", (TWO_FIXED, 0), (TWO_FIXED, 0), (TWO_FIXED, 0))
    broken = loop.Comparison(exit_code=2, lines=[], worse=[], better=[], report=Path("c"))

    adoptable, reason = loop.judge(base, head, broken)

    assert not adoptable
    assert reason == "highbar compare could not compare them (exit 2)"


def test_rounds_the_ceiling_skipped_make_a_side_unjudgeable(
    loop: ModuleType, tmp_path: Path
) -> None:
    run = loop.read_run(_report(tmp_path, "only", BASE, exit_code=1), 1)
    side = loop.Side("baseline", Path("p.md"), [run], 3)

    assert side.problem() == "2 of 3 rounds not run: the cycle ceiling"


def test_the_most_passes_wins_and_a_tie_goes_to_the_first(loop: ModuleType) -> None:
    assert loop.choose([(True, 2), (True, 3), (True, 3)]) == 1
    assert loop.choose([(False, 3), (True, 2)]) == 1
    assert loop.choose([(False, 3), (False, 2)]) is None


def test_a_run_starts_only_when_its_ceiling_fits_in_what_is_left(loop: ModuleType) -> None:
    assert loop.may_start(spent=0.0, max_cost=0.01, cycle_max_cost=0.01)
    assert loop.may_start(spent=0.02, max_cost=0.01, cycle_max_cost=0.03)
    assert not loop.may_start(spent=0.0041, max_cost=0.01, cycle_max_cost=0.01)


def test_a_run_with_no_known_cost_is_charged_its_ceiling(loop: ModuleType, tmp_path: Path) -> None:
    known = loop.read_run(_report(tmp_path, "k", BASE, cost={"USD": 0.004}), 0)
    unknown = loop.read_run(_report(tmp_path, "u", BASE), 0)
    two = loop.read_run(_report(tmp_path, "t", BASE, cost={"USD": 0.004, "EUR": 0.001}), 0)

    assert loop.charge(known, 0.01) == pytest.approx(0.004)
    assert loop.charge(unknown, 0.01) == 0.01
    assert loop.charge(two, 0.01) == 0.01


def test_a_missing_report_is_a_run_that_cannot_be_judged(loop: ModuleType, tmp_path: Path) -> None:
    run = loop.read_run(tmp_path / "never-written.json", 2)

    assert run.statuses == {}
    assert not run.complete
