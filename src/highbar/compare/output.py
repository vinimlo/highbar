"""Reading the sides from disk, and showing a comparison (HB-5)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from highbar.compare.core import ALPHA, FEW_ROUNDS, CompareError, Round, Row, Side, read_round

COMPARE_SCHEMA_VERSION = 1


def load_side(
    paths: Sequence[str], seen: dict[str, Path] | None = None
) -> list[dict[tuple[str, str], Round]]:
    """One round per JSON report: a file is one, a folder is every `*.json` in it.

    A report met twice (named twice, in a folder and by name, or copied) is
    refused: counted twice, one run would pass for two rounds. `seen` carries
    the reports already read, so the other side can be checked against them.
    """
    seen = {} if seen is None else seen
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found = sorted(path.glob("*.json"))
            if not found:
                raise CompareError(f"{raw}: no JSON report in this folder")
            files += found
        else:
            files.append(path)
    rounds: list[dict[tuple[str, str], Round]] = []
    for file in files:
        try:
            body = json.loads(file.read_bytes().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise CompareError(f"{file}: cannot read it as a JSON report ({exc})") from exc
        # By what it says, not its bytes: a copy written again with other spacing is
        # the same run. Two runs differ at least in started_at and duration_ms.
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if digest in seen:
            same = f"{file} and {seen[digest]} are the same report"
            raise CompareError(f"{same}: each round must be a run of its own")
        seen[digest] = file
        try:
            rounds.append(read_round(body))
        except CompareError as exc:
            raise CompareError(f"{file}: {exc}") from exc
    return rounds


def render(rows: Sequence[Row], console: Console) -> None:
    """One line per test; under a test that moved, why or why not, and the means that changed."""
    spec: str | None = None
    one_round = False
    for row in rows:
        if row.spec != spec:
            spec = row.spec
            console.print(f"\n  [bold]{escape(spec)}[/bold]", soft_wrap=True)
        if row.base is None or row.head is None or row.p is None:
            line = f"{escape(row.test)}: {escape(row.reason)}"
            console.print(f"    [dim]– {line}[/dim]", soft_wrap=True)
            continue
        mark = {"better": "[green]✓ better[/green]", "worse": "[red]✗ worse[/red]"}.get(
            row.verdict or "", "[yellow]· no verdict[/yellow]"
        )
        p_holm = f" · Holm p {row.p_holm:.3g}" if row.p_holm is not None else ""
        console.print(
            f"    {mark}  {escape(row.test)}  base {_rate(row.base)} → head {_rate(row.head)}"
            f" · p {row.p:.3g}{p_holm}",
            soft_wrap=True,
        )
        if row.verdict is not None and (row.base.errors or row.head.errors):
            console.print(f"      → {escape(row.reason)}", soft_wrap=True)
        if row.verdict is None and (row.p < ALPHA or row.base_errors_decide):
            reason = row.reason
            if reason == FEW_ROUNDS:
                one_round = True
                reason = FEW_ROUNDS.split(";")[0] + " (see below)"
            console.print(f"      → {escape(reason)}", soft_wrap=True)
        means = _means(row.base, row.head)
        if means and _moved(row.base, row.head):
            console.print(f"      [dim]{escape(means)}[/dim]", soft_wrap=True)
    compared = [r for r in rows if r.p is not None]
    counts = [
        f"{len(compared)} compared",
        f"{sum(r.verdict == 'better' for r in rows)} better",
        f"{sum(r.verdict == 'worse' for r in rows)} worse",
        f"{sum(r.verdict is None for r in compared)} no verdict",
    ]
    skipped = len(rows) - len(compared)
    if skipped:
        counts.append(f"{skipped} not compared")
    console.print("\n  " + " | ".join(counts), soft_wrap=True)
    if one_round:
        console.print(f"  {escape(FEW_ROUNDS)}.", soft_wrap=True)


def write_json(path: str, rows: Sequence[Row], base: Sequence[str], head: Sequence[str]) -> None:
    body = {
        "schema_version": COMPARE_SCHEMA_VERSION,
        "base": list(base),
        "head": list(head),
        "tests": [_row_json(r) for r in rows],
    }
    Path(path).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _rate(side: Side) -> str:
    low, high = side.interval
    rounds = side.measured_rounds
    text = f"{side.passed}/{side.executions} in {rounds} round{'s' if rounds != 1 else ''}"
    text += f" ({low:.0%}–{high:.0%})"
    if side.errors:
        text += f", {side.errors} of them error"
    return text


def _means(base: Side, head: Side) -> str:
    parts: list[str] = []
    for currency in sorted(set(base.mean_cost) | set(head.mean_cost)):
        before, after = base.mean_cost.get(currency), head.mean_cost.get(currency)
        parts.append(f"{currency} {_num(before, '.4g')} → {_num(after, '.4g')}")
    if base.mean_input_tokens is not None or head.mean_input_tokens is not None:
        before = _num(base.mean_input_tokens, ".0f")
        parts.append(f"input tokens {before} → {_num(head.mean_input_tokens, '.0f')}")
    if base.mean_ms is not None and head.mean_ms is not None:
        parts.append(f"{base.mean_ms / 1000:.2f}s → {head.mean_ms / 1000:.2f}s")
    return "per execution: " + " · ".join(parts) if parts else ""


def _moved(base: Side, head: Side) -> bool:
    """Whether a mean differs by more than 5%: below that, the line is noise."""
    currencies = {*base.mean_cost, *head.mean_cost}
    pairs = [
        (base.mean_ms, head.mean_ms),
        (base.mean_input_tokens, head.mean_input_tokens),
        *((base.mean_cost.get(c), head.mean_cost.get(c)) for c in currencies),
    ]
    for before, after in pairs:
        if (before is None) != (after is None):
            return True
        if before is not None and after is not None and abs(after - before) > 0.05 * abs(before):
            return True
    return False


def _num(value: float | None, spec: str) -> str:
    return "?" if value is None else format(value, spec)


def _side_json(side: Side | None) -> dict[str, Any] | None:
    if side is None:
        return None
    low, high = side.interval
    return {
        "rounds": [
            {"passed": r.passed, "failed": r.failed, "error": r.errors} for r in side.rounds
        ],
        "passed": side.passed,
        "executions": side.executions,
        "error": side.errors,
        "interval": [low, high],
        "rounds_disagree_p": side.disagree_p,
        "mean_ms": side.mean_ms,
        "mean_input_tokens": side.mean_input_tokens,
        "mean_output_tokens": side.mean_output_tokens,
        "mean_cost": _plain(side.mean_cost),
    }


def _plain(costs: Mapping[str, float]) -> dict[str, float]:
    return {currency: round(amount, 10) for currency, amount in costs.items()}


def _row_json(row: Row) -> dict[str, Any]:
    return {
        "spec": row.spec,
        "test": row.test,
        "compared": row.p is not None,
        "p": row.p,
        "p_holm": row.p_holm,
        "verdict": row.verdict,
        "reason": row.reason,
        "base": _side_json(row.base),
        "head": _side_json(row.head),
    }
