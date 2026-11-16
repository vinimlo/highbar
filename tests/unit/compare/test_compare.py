"""Matching reports and the verdict rule of `highbar compare` (HB-5)."""

from __future__ import annotations

from typing import Any

import pytest

from highbar.compare.core import CompareError, compare, read_round


def _report(*tests: tuple[Any, ...], sha: str = "d1") -> dict[str, Any]:
    """A `--report json=` body with one spec; each test is (id, passed, failed[, error])."""
    body: list[dict[str, Any]] = []
    for test in tests:
        test_id, passed, failed, *rest = test
        error = rest[0] if rest else 0
        body.append(
            {
                "id": test_id,
                "definition_sha256": sha,
                "counts": {"passed": passed, "failed": failed, "error": error},
                "duration_ms": 1000.0 * (passed + failed + error),
                "totals": {"input_tokens": 100, "output_tokens": 10, "cost": {"USD": 0.01}},
            }
        )
    return {"schema_version": 1, "specs": [{"key": "specs/sink.highbar.yaml", "tests": body}]}


def _sink(passed: int, failed: int = -1) -> dict[str, Any]:
    return _report(("warranty", passed, 5 - passed if failed < 0 else failed))


def _row(rows: Any, test_id: str = "warranty") -> Any:
    return next(r for r in rows if r.test == test_id)


def test_his_case_one_round_each_is_no_conclusion() -> None:
    # 1/5 against 5/5: raw Fisher p = 0.048, and still no verdict.
    rows = compare([read_round(_sink(1))], [read_round(_sink(5))])
    row = _row(rows)
    assert row.p == pytest.approx(0.0476, abs=1e-4)
    assert row.verdict is None
    assert "one round" in row.reason
    assert "4 rounds of 5 rather than 1 of 20" in row.reason


def test_his_seven_tests_holm_takes_it_to_0_33() -> None:
    # Two rounds a side, so only Holm stands between p = 0.03 and a verdict.
    others = [(n, 5, 0) for n in "abcdef"]
    base = [
        read_round(_report(("warranty", 2, 3), *others)),
        read_round(_report(("warranty", 3, 2), *others)),
    ]
    head = [read_round(_report(("warranty", 5, 0), *others))] * 2
    rows = compare(base, head)
    row = _row(rows)
    assert row.p < 0.05
    assert row.p_holm > 0.05
    assert row.verdict is None
    assert "Holm" in row.reason


def test_rounds_that_disagree_are_a_block_effect() -> None:
    base = [read_round(_sink(1)), read_round(_sink(5))]
    rows = compare(base, [read_round(_sink(5))] * 2)
    row = _row(rows)
    assert row.verdict is None
    assert "rounds disagree" in row.reason
    assert "base" in row.reason


def test_a_real_regression_is_worse() -> None:
    base = [read_round(_report(("warranty", 10, 0))) for _ in range(4)]
    head = [read_round(_report(("warranty", 1, 9))) for _ in range(4)]
    row = _row(compare(base, head))
    assert row.verdict == "worse"
    assert row.p_holm < 0.05


def test_a_real_improvement_is_better() -> None:
    base = [read_round(_report(("warranty", 1, 9))) for _ in range(4)]
    head = [read_round(_report(("warranty", 10, 0))) for _ in range(4)]
    assert _row(compare(base, head)).verdict == "better"


def test_review_errors_count_as_not_passed() -> None:
    # Review of #69: head 5 passed / 5 error a round against base 5/10 read "better".
    base = [read_round(_report(("warranty", 5, 5))) for _ in range(2)]
    head = [read_round(_report(("warranty", 5, 0, 5))) for _ in range(2)]
    row = _row(compare(base, head))
    assert (row.head.passed, row.head.executions, row.head.errors) == (10, 20, 10)
    assert row.verdict is None


def test_review_more_errors_on_head_is_worse() -> None:
    # Review of #69: 1 passed / 9 error against 10/10 was "no verdict", p 1.
    base = [read_round(_report(("warranty", 10, 0))) for _ in range(4)]
    head = [read_round(_report(("warranty", 1, 0, 9))) for _ in range(4)]
    row = _row(compare(base, head))
    assert row.verdict == "worse"
    assert "36 error" in row.reason


def test_review_an_error_never_makes_the_head_look_better() -> None:
    # Review of #69 at 7976aea, note (b): a base that errors everywhere made any head "better".
    base = [read_round(_report(("warranty", 0, 0, 10))) for _ in range(4)]
    head = [read_round(_report(("warranty", 10, 0))) for _ in range(4)]
    row = _row(compare(base, head))
    assert row.verdict is None
    assert "40 error executions on base" in row.reason


def test_a_head_better_beyond_the_bases_errors_is_still_better() -> None:
    # Base: 2 passed, 7 failed, 1 error a round. Even with its errors as passes it is
    # 12/40 against 40/40, so the head is better whichever way they went.
    base = [read_round(_report(("warranty", 2, 7, 1))) for _ in range(4)]
    head = [read_round(_report(("warranty", 10, 0))) for _ in range(4)]
    assert _row(compare(base, head)).verdict == "better"


def test_a_changed_definition_is_not_compared() -> None:
    base = [read_round(_report(("warranty", 1, 9), sha="old")) for _ in range(4)]
    head = [read_round(_report(("warranty", 10, 0), sha="new")) for _ in range(4)]
    same = [read_round(_report(("menu", 5, 0)))] * 4
    base = [{**b, **s} for b, s in zip(base, same, strict=True)]
    head = [{**h, **s} for h, s in zip(head, same, strict=True)]
    row = _row(compare(base, head))
    assert row.p is None
    assert row.verdict is None
    assert "definition changed" in row.reason


def test_a_test_on_one_side_only_is_not_compared() -> None:
    base = [read_round(_report(("warranty", 5, 0), ("gone", 5, 0)))] * 2
    head = [read_round(_report(("warranty", 5, 0)))] * 2
    row = _row(compare(base, head), "gone")
    assert row.p is None
    assert "not in every report" in row.reason


def test_nothing_in_common_is_an_error() -> None:
    with pytest.raises(CompareError, match="no test in common"):
        compare([read_round(_report(("a", 5, 0)))], [read_round(_report(("b", 5, 0)))])


def test_an_unreadable_report_is_an_error() -> None:
    with pytest.raises(CompareError, match="specs"):
        read_round({"schema_version": 1})


def test_cost_tokens_and_latency_are_means_per_execution() -> None:
    row = _row(compare([read_round(_sink(1))], [read_round(_sink(5))]))
    assert row.base.mean_ms == pytest.approx(1000.0)
    assert row.base.mean_cost == {"USD": pytest.approx(0.002)}
    assert row.base.mean_input_tokens == pytest.approx(20.0)
