"""Pair two groups of `--report json=` runs by test, and say what holds (HB-5).

Each report is one round. Rounds stay apart: the block check needs them, and
pooling 1 round of 20 hides the variation that 4 rounds of 5 measure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from highbar._narrow import as_dict, as_list
from highbar.compare.stats import fisher_exact, holm, rounds_disagree_p, wilson

ALPHA = 0.05
"""For the Holm-adjusted p of a verdict, and for the rounds-disagree check."""

ONE_ROUND = (
    "one round on a side: no conclusion; run 4 rounds of 5 rather than 1 of 20, "
    "since only that measures the variation between rounds"
)

Verdict = Literal["better", "worse"]


class CompareError(Exception):
    """A report that cannot be read, or two sides with nothing to compare."""


@dataclass(frozen=True)
class Round:
    """One test in one report."""

    definition: str
    passed: int
    failed: int
    errors: int
    duration_ms: float
    input_tokens: int | None
    output_tokens: int | None
    cost: Mapping[str, float]


@dataclass(frozen=True)
class Side:
    """One test over the rounds of one side."""

    rounds: tuple[Round, ...]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.rounds)

    @property
    def judged(self) -> int:
        """Passed and failed: an `error` execution verified nothing, so it is not here."""
        return sum(r.passed + r.failed for r in self.rounds)

    @property
    def errors(self) -> int:
        return sum(r.errors for r in self.rounds)

    @property
    def executions(self) -> int:
        return self.judged + self.errors

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.passed, self.judged)

    @property
    def disagree_p(self) -> float:
        return rounds_disagree_p([(r.passed, r.passed + r.failed) for r in self.rounds])

    @property
    def mean_ms(self) -> float | None:
        return _mean(sum(r.duration_ms for r in self.rounds), self.executions)

    @property
    def mean_input_tokens(self) -> float | None:
        return _mean_known([r.input_tokens for r in self.rounds], self.executions)

    @property
    def mean_output_tokens(self) -> float | None:
        return _mean_known([r.output_tokens for r in self.rounds], self.executions)

    @property
    def mean_cost(self) -> dict[str, float]:
        sums: dict[str, float] = {}
        for r in self.rounds:
            for currency, amount in r.cost.items():
                sums[currency] = sums.get(currency, 0.0) + amount
        if not self.executions:
            return {}
        return {c: amount / self.executions for c, amount in sums.items()}


@dataclass(frozen=True)
class Row:
    """One test's comparison. `p` is None when the test was not compared."""

    spec: str
    test: str
    base: Side | None
    head: Side | None
    p: float | None = None
    p_holm: float | None = None
    verdict: Verdict | None = None
    reason: str = ""
    notes: list[str] = field(default_factory=list[str])


def read_round(body: Any) -> dict[tuple[str, str], Round]:
    """One `--report json=` body as {(spec key, test id): Round}."""
    report = as_dict(body)
    if report is None or not isinstance(report.get("specs"), list):
        raise CompareError("not a highbar JSON report: it has no `specs` list")
    tests: dict[tuple[str, str], Round] = {}
    for spec in as_list(report["specs"]) or []:
        spec_body = as_dict(spec) or {}
        key = str(spec_body.get("key") or spec_body.get("path") or "")
        for test in as_list(spec_body.get("tests")) or []:
            t = as_dict(test) or {}
            counts = as_dict(t.get("counts")) or {}
            totals = as_dict(t.get("totals")) or {}
            cost = as_dict(totals.get("cost")) or {}
            tests[(key, str(t.get("id")))] = Round(
                definition=str(t.get("definition_sha256")),
                passed=_int(counts.get("passed")),
                failed=_int(counts.get("failed")),
                errors=_int(counts.get("error")),
                duration_ms=float(t.get("duration_ms") or 0.0),
                input_tokens=_maybe_int(totals.get("input_tokens")),
                output_tokens=_maybe_int(totals.get("output_tokens")),
                cost={str(c): float(v) for c, v in cost.items() if isinstance(v, int | float)},
            )
    return tests


def compare(
    base: Sequence[Mapping[tuple[str, str], Round]],
    head: Sequence[Mapping[tuple[str, str], Round]],
) -> list[Row]:
    """Every test of either side, compared where it can be, with Holm across the compared."""
    if not base or not head:
        raise CompareError("each side needs at least one report")
    keys = sorted({k for r in [*base, *head] for k in r})
    rows: list[Row] = []
    for spec, test in keys:
        key = (spec, test)
        if not all(key in r for r in [*base, *head]):
            rows.append(Row(spec, test, None, None, reason="not in every report: not compared"))
            continue
        b = Side(tuple(r[key] for r in base))
        h = Side(tuple(r[key] for r in head))
        if len({r.definition for r in [*b.rounds, *h.rounds]}) > 1:
            changed = "definition changed between reports: not compared"
            rows.append(Row(spec, test, b, h, reason=changed))
            continue
        rows.append(Row(spec, test, b, h, p=fisher_exact(b.passed, b.judged, h.passed, h.judged)))
    compared = [r for r in rows if r.p is not None]
    if not compared:
        raise CompareError("no test in common with the same definition on both sides")
    adjusted = holm([r.p for r in compared if r.p is not None])
    by_key = {(r.spec, r.test): p for r, p in zip(compared, adjusted, strict=True)}
    return [_judge(r, by_key[(r.spec, r.test)]) if r.p is not None else r for r in rows]


def _judge(row: Row, p_holm: float) -> Row:
    assert row.base is not None and row.head is not None and row.p is not None
    base, head = row.base, row.head
    reason = ""
    verdict: Verdict | None = None
    split = [name for name, side in (("base", base), ("head", head)) if side.disagree_p < ALPHA]
    if base.judged == 0 or head.judged == 0:
        reason = "no execution verified on a side: no conclusion"
    elif min(len(base.rounds), len(head.rounds)) < 2:
        reason = ONE_ROUND
    elif split:
        reason = f"rounds disagree on {' and '.join(split)}: block effect, no conclusion"
    elif p_holm >= ALPHA:
        reason = f"no difference beyond chance (p = {row.p:.3g}, Holm p = {p_holm:.3g})"
    else:
        better = head.passed / head.judged > base.passed / base.judged
        verdict = "better" if better else "worse"
        reason = f"Holm p = {p_holm:.3g}"
    return Row(row.spec, row.test, base, head, row.p, p_holm, verdict, reason)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _maybe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mean(total: float, count: int) -> float | None:
    return total / count if count else None


def _mean_known(values: list[int | None], count: int) -> float | None:
    """None when any round left it unknown: a mean over part of the rounds would mislead."""
    if any(v is None for v in values):
        return None
    return _mean(float(sum(v for v in values if v is not None)), count)
