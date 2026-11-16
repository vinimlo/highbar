"""Try 1 or 2 candidate prompts against the current one, and adopt only what highbar supports.

    python loop.py --prompt prompt.md prompts/a.md prompts/b.md \\
        --max-cost 0.01 --cycle-max-cost 0.1

One cycle: the suite runs with the current prompt in --rounds rounds, then with each
candidate in as many, and `highbar compare` sets each candidate against the baseline. A
candidate is adopted only when compare finds no regression, it passes more tests than
the baseline, and nothing ended in `error` on either side. No model judges anything.
Every cycle is appended to EXPERIENCE.md. Standard library only; highbar is called as a
command. The README says what each step does and why.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

EXIT_ADOPTED, EXIT_NOTHING_ADOPTED, EXIT_UNRUNNABLE = 0, 1, 2
# highbar.compare.core.MIN_ROUNDS: below it, compare gives no verdict. Mirrored, not
# imported, so the loop runs wherever the `highbar` command does (a container, a venv).
MIN_ROUNDS = 3
ANSI = re.compile(r"\x1b\[[0-9;]*m")
STOPS = {
    2: "could not run",
    3: "was stopped by --max-cost",
    4: "was stopped by --max-errors",
    130: "was interrupted",
}


@dataclass(frozen=True)
class Run:
    """One `highbar run`: its exit code and what its JSON report says."""

    report: Path
    exit_code: int
    statuses: dict[str, str]  # test -> passed, failed or error, in the report's order
    complete: bool
    cost: dict[str, float] | None  # per currency; None when the report has no totals


@dataclass
class Side:
    """One prompt over its rounds."""

    label: str
    prompt: Path
    runs: list[Run]
    rounds: int

    def passed(self) -> list[str]:
        """The tests that passed in every round."""
        if not self.runs:
            return []
        first = self.runs[0].statuses
        return [t for t in first if all(r.statuses.get(t) == "passed" for r in self.runs)]

    def tests(self) -> list[str]:
        return list(self.runs[0].statuses) if self.runs else []

    def failures(self) -> list[str]:
        """Each test that did not pass in every round, with how it ended."""
        lines: list[str] = []
        for test in self.tests():
            ends = sorted({r.statuses.get(test, "missing") for r in self.runs} - {"passed"})
            if ends:
                lines.append(f"{test} ({', '.join(ends)})")
        return lines

    def problem(self) -> str | None:
        """Why this side cannot be judged, or None."""
        if len(self.runs) < self.rounds:
            missing = self.rounds - len(self.runs)
            return f"{missing} of {self.rounds} rounds not run: the cycle ceiling"
        for number, run in enumerate(self.runs, start=1):
            if run.exit_code not in (0, 1):
                stop = STOPS.get(run.exit_code, "ended")
                return f"round {number} {stop} (exit {run.exit_code})"
            if not run.complete or not run.statuses:
                return f"round {number} did not run every test"
            for test, status in run.statuses.items():
                if status == "error":
                    return f"{test} ended in error in round {number}"
        return None

    def score(self) -> str:
        return f"{len(self.passed())}/{len(self.tests())}"


@dataclass(frozen=True)
class Comparison:
    """What `highbar compare --fail-on-regression` said about a candidate."""

    exit_code: int
    lines: list[str]  # its better and worse lines and its count line, as printed
    worse: list[str]
    better: list[str]
    report: Path
    no_verdict: list[str] = field(default_factory=list[str])  # compared, and no verdict


@dataclass
class Cycle:
    args: argparse.Namespace
    folder: Path
    highbar: list[str]
    spent: float = 0.0
    currency: str = ""
    runs: int = 0
    skipped: list[str] = field(default_factory=list[str])


def read_run(report: Path, exit_code: int) -> Run:
    """A run from its report. A report that is missing or unreadable is a run with no tests."""
    try:
        body = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Run(report, exit_code, {}, False, None)
    specs = body.get("specs") or []
    statuses: dict[str, str] = {}
    for spec in specs:
        for test in spec.get("tests") or []:
            name = str(test.get("id"))
            if len(specs) > 1:
                name = f"{spec.get('key')}: {name}"
            statuses[name] = str(test.get("status"))
    totals = body.get("totals")
    cost: dict[str, float] | None = None
    if isinstance(totals, dict) and totals.get("complete"):
        cost = {str(c): float(v) for c, v in (totals.get("cost") or {}).items()}
    return Run(report, exit_code, statuses, bool(body.get("complete")), cost)


def charge(run: Run, max_cost: float) -> float:
    """What a run counts against the cycle: its cost, or its ceiling when that is unknown.

    Unknown is a report with no totals, totals that left something out, no cost (tokens
    for a model the price table lacks), or costs in more than one currency, which one
    ceiling cannot hold.
    """
    if run.cost is None or len(run.cost) != 1:
        return max(max_cost, sum(run.cost.values()) if run.cost else 0.0)
    return sum(run.cost.values())


def may_start(spent: float, max_cost: float, cycle_max_cost: float) -> bool:
    """A run starts only if its own ceiling still fits in what the cycle has left."""
    return spent + max_cost <= cycle_max_cost + 1e-12


def judge(baseline: Side, candidate: Side, comparison: Comparison | None) -> tuple[bool, str]:
    """Whether the candidate can be adopted, and why."""
    problem = baseline.problem()
    if problem:
        return False, f"the baseline cannot be judged: {problem}"
    problem = candidate.problem()
    if problem:
        return False, problem
    # The guard that needs no statistics: compare exits 0 with no verdict when it has too
    # few executions to tell, and a test that passed every time must not be traded away.
    kept = set(candidate.passed())
    lost = [test for test in baseline.passed() if test not in kept]
    reasons: list[str] = []
    if lost:
        reasons.append(f"lost a test that passed in every baseline round: {', '.join(lost)}")
    if comparison is None or comparison.exit_code not in (0, 1):
        code = comparison.exit_code if comparison else "none"
        reasons.append(f"highbar compare could not compare them (exit {code})")
    elif comparison.worse or comparison.exit_code == 1:
        worse = ", ".join(comparison.worse) or "see its output"
        reasons.append(f"highbar compare found a regression: {worse}")
    if reasons or comparison is None:
        return False, "; ".join(reasons)
    total = len(candidate.tests())
    passed, before = len(candidate.passed()), len(baseline.passed())
    if passed <= before:
        return False, f"{passed} of {total} tests passed, not more than the baseline's {before}"
    # What was established, and no more: "no verdict" is not "no regression".
    return True, (
        f"{passed} of {total} tests passed, the baseline {before}; "
        "no test that passed in the baseline was lost; "
        f"highbar compare: {len(comparison.better)} better, {len(comparison.worse)} worse, "
        f"{len(comparison.no_verdict)} no verdict"
    )


def choose(verdicts: list[tuple[bool, int]]) -> int | None:
    """The adoptable candidate with the most passes; on a tie, the first. None: none."""
    best: int | None = None
    for index, (adoptable, passes) in enumerate(verdicts):
        if adoptable and (best is None or passes > verdicts[best][1]):
            best = index
    return best


def relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def run_side(cycle: Cycle, label: str, name: str, prompt: Path) -> Side:
    """The suite with this prompt, in --rounds rounds, while the cycle ceiling allows."""
    args = cycle.args
    folder = cycle.folder / name
    folder.mkdir(parents=True)
    side = Side(label, prompt, [], args.rounds)
    print(f"\n{label} ({relative(prompt)})")
    for number in range(1, args.rounds + 1):
        where = f"{name} round {number}/{args.rounds}"
        if not may_start(cycle.spent, args.max_cost, args.cycle_max_cost):
            note = (
                f"not started: {cycle.spent:.4g} spent + {args.max_cost:g} for this run "
                f"would pass the cycle's {args.cycle_max_cost:g}"
            )
            print(f"  {where}: {note}")
            cycle.skipped.append(f"{where}: {note}")
            break
        report = folder / f"{number}.json"
        command = [*cycle.highbar, "run", args.spec, "--report", f"json={report}"]
        command += ["--max-cost", f"{args.max_cost:g}"]
        env = {**os.environ, args.prompt_env: str(prompt.resolve())}
        done = subprocess.run(command, env=env, capture_output=True, text=True)
        (folder / f"{number}.log").write_text(done.stdout + done.stderr, encoding="utf-8")
        run = read_run(report, done.returncode)
        side.runs.append(run)
        charged = charge(run, args.max_cost)
        cycle.spent += charged
        cycle.runs += 1
        if run.cost and len(run.cost) == 1:
            cycle.currency = next(iter(run.cost))
        cost = f"{charged:.4g} {cycle.currency}".rstrip()
        if run.cost is None or len(run.cost) != 1:
            cost += " (unknown: charged its ceiling)"
        passed = sum(s == "passed" for s in run.statuses.values())
        print(
            f"  {where}: exit {run.exit_code} · {passed} of {len(run.statuses)} tests passed"
            f" · {cost} · {relative(report)}"
        )
    return side


def compare(cycle: Cycle, name: str) -> Comparison:
    """`highbar compare --fail-on-regression`, the baseline's rounds against this side's."""
    report = cycle.folder / f"compare-{name}.json"
    command = [*cycle.highbar, "compare", "--base", str(cycle.folder / "baseline")]
    command += ["--head", str(cycle.folder / name), "--fail-on-regression"]
    command += ["--report", f"json={report}"]
    done = subprocess.run(command, capture_output=True, text=True)
    output = ANSI.sub("", done.stdout + done.stderr)
    (cycle.folder / f"compare-{name}.log").write_text(output, encoding="utf-8")
    lines = [
        line.strip()
        for line in output.splitlines()
        if "✓ better" in line or "✗ worse" in line or " compared |" in line
    ]
    verdicts: dict[str, list[str]] = {"better": [], "worse": [], "none": []}
    try:
        for row in json.loads(report.read_text(encoding="utf-8"))["tests"]:
            verdict = row.get("verdict") or ("none" if row.get("compared") else None)
            if verdict in verdicts:
                verdicts[verdict].append(str(row["test"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # no report: the exit code says compare could not run
    worse, better, none = verdicts["worse"], verdicts["better"], verdicts["none"]
    return Comparison(done.returncode, lines, worse, better, report, none)


def propose(cycle: Cycle, baseline: Side) -> list[Path] | None:
    """Run --propose CMD BASELINE_REPORT CURRENT_PROMPT OUT_DIR; the files it wrote."""
    out = cycle.folder / "proposed"
    out.mkdir()
    command = [*shlex.split(cycle.args.propose), str(baseline.runs[0].report)]
    command += [str(cycle.args.prompt), str(out)]
    print(f"\npropose: {shlex.join(command)}")
    done = subprocess.run(command)
    written = sorted(p for p in out.iterdir() if p.is_file())
    if done.returncode != 0:
        print(f"  the proposer exited with {done.returncode}: no candidate", file=sys.stderr)
        return None
    if not 1 <= len(written) <= 2:
        print(f"  the proposer wrote {len(written)} files in {relative(out)}; it must write 1 or 2")
        return None
    return written


def experience(
    cycle: Cycle,
    baseline: Side,
    judged: list[tuple[Side, Comparison | None, bool, str]],
    adopted: int | None,
) -> None:
    """Append the cycle to EXPERIENCE.md: situation, each candidate, decision, evidence."""
    path = Path(cycle.args.experience)
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    title = "nothing adopted"
    if adopted is not None:
        title = f"adopted {relative(judged[adopted][0].prompt)}"
    lines = [f"## {when}: {title}", ""]
    if baseline.runs:
        lines.append(
            f"Situation: `{relative(cycle.args.prompt)}` passed {baseline.score()} tests in "
            f"every one of {len(baseline.runs)} rounds."
        )
        failures = baseline.failures()
        lines += ["Failing:", *(f"- {f}" for f in failures)] if failures else ["Nothing failed."]
    problem = baseline.problem()
    if problem:
        lines.append(f"The baseline cannot be judged: {problem}.")
    for skip in cycle.skipped:
        lines.append(f"Not run, the cycle ceiling: {skip}.")
    lines.append(f"Baseline reports: {_paths(baseline)}")
    for index, (side, comparison, _, reason) in enumerate(judged):
        decision = "rejected"
        if index == adopted:
            decision = "adopted (dry run, not copied)" if cycle.args.dry_run else "adopted"
        lines += ["", f"### {relative(side.prompt)}: {decision}", "", f"Why: {reason}.", ""]
        lines.append("Evidence:")
        lines.append(
            f"- {side.score()} tests passed (baseline {baseline.score()}), "
            f"in {len(side.runs)} of {side.rounds} rounds"
        )
        if comparison is not None:
            lines.append(f"- highbar compare, exit {comparison.exit_code}:")
            lines += [f"      {line}" for line in comparison.lines]
            lines.append(f"- compare report: `{relative(comparison.report)}`")
        lines.append(f"- reports: {_paths(side)}")
    spent = f"{cycle.spent:.4g} {cycle.currency}".rstrip()
    ceiling = f"{cycle.args.cycle_max_cost:g}"
    lines += ["", f"Spent: {spent} of the cycle's {ceiling}, in {cycle.runs} runs.", ""]
    if not path.exists():
        path.write_text(
            "# Experience\n\nWhat each cycle of loop.py tried, what it decided and why. "
            "Newest last.\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as file:
        file.write("\n" + "\n".join(lines))


def _paths(side: Side) -> str:
    return ", ".join(f"`{relative(r.report)}`" for r in side.runs) or "none"


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidates", nargs="*", type=Path, help="1 or 2 candidate prompts")
    parser.add_argument("--prompt", type=Path, required=True, help="the current prompt")
    parser.add_argument(
        "--propose",
        metavar="CMD",
        help="writes the candidates: run as CMD BASELINE_REPORT CURRENT_PROMPT OUT_DIR",
    )
    parser.add_argument("--max-cost", type=float, required=True, help="each run's ceiling")
    parser.add_argument("--cycle-max-cost", type=float, required=True, help="the cycle's")
    parser.add_argument("--rounds", type=int, default=MIN_ROUNDS, help="runs a side (at least 3)")
    parser.add_argument("--spec", default="specs", help="spec or folder (default specs)")
    parser.add_argument("--prompt-env", default="PROMPT_FILE", help="names the prompt's path")
    parser.add_argument("--runs-dir", type=Path, default=Path("loop-runs"))
    parser.add_argument("--experience", type=Path, default=Path("EXPERIENCE.md"))
    parser.add_argument("--highbar", default="highbar", help="the command (default highbar)")
    parser.add_argument("--dry-run", action="store_true", help="print the winner, copy nothing")
    args = parser.parse_args(argv)
    if bool(args.candidates) == bool(args.propose):
        parser.error("give 1 or 2 candidate prompts, or --propose CMD")
    if len(args.candidates) > 2:
        parser.error("at most 2 candidates a cycle")
    if args.rounds < MIN_ROUNDS:
        parser.error(
            f"--rounds must be at least {MIN_ROUNDS}: with fewer, highbar compare gives no verdict"
        )
    for path in [args.prompt, *args.candidates]:
        if not path.is_file():
            parser.error(f"{path}: no such file")
    return args


def main(argv: list[str]) -> int:
    args = parse(argv)
    folder = args.runs_dir / datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    cycle = Cycle(args, folder, shlex.split(args.highbar))
    print(
        f"prompt-loop: {args.spec}, {args.rounds} rounds a side, --max-cost {args.max_cost:g} "
        f"a run, {args.cycle_max_cost:g} for the cycle · {relative(folder)}"
    )
    baseline = run_side(cycle, "baseline", "baseline", args.prompt)
    candidates: list[Path] = list(args.candidates)
    judged: list[tuple[Side, Comparison | None, bool, str]] = []
    problem = baseline.problem()
    if problem:
        print(f"\nthe baseline cannot be judged: {problem}; no candidate is run")
    elif args.propose:
        proposed = propose(cycle, baseline)
        if proposed is None:
            return EXIT_UNRUNNABLE
        candidates = proposed
    if not problem:
        for number, prompt in enumerate(candidates, start=1):
            name = f"candidate-{number}"
            side = run_side(cycle, f"candidate {number}", name, prompt)
            comparison = None
            # Compared whenever every round has a report: an error is evidence too.
            if len(side.runs) == side.rounds and all(r.statuses for r in side.runs):
                comparison = compare(cycle, name)
                print(f"  compare: exit {comparison.exit_code}")
                for line in comparison.lines:
                    print(f"    {line}")
            adoptable, reason = judge(baseline, side, comparison)
            print(f"  {relative(prompt)}: {side.score().replace('/', ' of ')} tests passed")
            judged.append((side, comparison, adoptable, reason))

    adopted = choose([(ok, len(side.passed())) for side, _, ok, _ in judged])
    print()
    for index, (side, _, _, reason) in enumerate(judged):
        if index != adopted:
            print(f"rejected {relative(side.prompt)}: {reason}")
    if adopted is None:
        print("nothing adopted")
    else:
        side, _, _, reason = judged[adopted]
        print(f"adopted {relative(side.prompt)}: {reason}")
        if args.dry_run:
            print(f"dry run: {relative(args.prompt)} left as it was. The adopted prompt:\n")
            print(side.prompt.read_text(encoding="utf-8"))
        else:
            shutil.copyfile(side.prompt, args.prompt)
            print(f"copied over {relative(args.prompt)}")
    experience(cycle, baseline, judged, adopted)
    spent = f"{cycle.spent:.4g} {cycle.currency}".rstrip()
    print(f"spent {spent} of {args.cycle_max_cost:g} in {cycle.runs} runs")
    print(f"{relative(args.experience)}: cycle appended")
    return EXIT_NOTHING_ADOPTED if adopted is None else EXIT_ADOPTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
