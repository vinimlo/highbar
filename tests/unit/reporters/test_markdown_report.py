"""HB-19: the report as Markdown, for a CI job's summary page ($GITHUB_STEP_SUMMARY).

It is the JSON report's run, in another format: replaced before the first message,
written on every way out, and never "passed" when the run was not.
"""

from __future__ import annotations

import re
from pathlib import Path

from highbar.domain.models import AgentConfig, Counts, RunSummary, Spec, Status, Suite
from highbar.domain.models import Test as Case
from highbar.domain.models import TestOutcome as Outcome
from highbar.reporters.markdown_report import ROW_LIMIT, MarkdownReport, render

SPEC = Spec(
    agent=AgentConfig(endpoint="http://x/chat"), suites=[Suite(name="Orders", tests=[Case()])]
)


def _outcome(name: str, status: Status, *errors: str, duration_ms: float = 1500.0) -> Outcome:
    return Outcome(
        id=f"Orders/{name}",
        suite="Orders",
        name=name,
        definition_sha256="ab" * 32,
        status=status,
        runs=1,
        min_pass=1,
        counts=Counts(
            passed=int(status is Status.PASSED),
            failed=int(status is Status.FAILED),
            error=int(status is Status.ERROR),
        ),
        duration_ms=duration_ms,
        errors=errors,
    )


def _summary(*tests: Outcome) -> RunSummary:
    return RunSummary(
        total=len(tests),
        passed=sum(t.status is Status.PASSED for t in tests),
        failed=sum(t.status is Status.FAILED for t in tests),
        errored=sum(t.status is Status.ERROR for t in tests),
        duration_ms=sum(t.duration_ms for t in tests),
        tests=tests,
    )


def _run(tmp_path: Path, *tests: Outcome, code: int) -> str:
    report = MarkdownReport(tmp_path / "summary.md", version="0.2.0")
    report.start()
    report.add("specs/orders.highbar.yaml", SPEC, _summary(*tests), complete=True)
    report.finish(code)
    return report.path.read_text(encoding="utf-8")


def _rows(text: str) -> list[str]:
    """The table rows, without the headers and the separators."""
    headers = ("| Result |", "| Test |", "| --- |")
    return [
        line for line in text.splitlines() if line.startswith("| ") and not line.startswith(headers)
    ]


def test_hb19_an_unfinished_run_says_so(tmp_path: Path) -> None:
    """A run that dies leaves this file: the summary page must not read as a pass."""
    report = MarkdownReport(tmp_path / "summary.md", version="0.2.0")

    report.start()

    text = report.path.read_text(encoding="utf-8")
    assert text.startswith("## highbar: the run did not finish\n")
    assert "passed" not in text.splitlines()[0]
    assert "highbar stopped before writing its result" in text


def test_hb19_a_green_run_is_passed_with_its_tests_folded(tmp_path: Path) -> None:
    text = _run(
        tmp_path,
        _outcome("looks the order up", Status.PASSED),
        _outcome("cancels it", Status.PASSED, duration_ms=250.0),
        code=0,
    )

    assert text.startswith("## highbar: passed\n")
    # The run's own duration, as the terminal and the JUnit root show it.
    assert re.search(r"^\*\*2 passed\*\* · \d+\.\d\ds$", text, re.MULTILINE)
    assert "<details><summary>2 passed</summary>" in text
    assert "| Orders › looks the order up | specs/orders.highbar.yaml | 1.50s |" in text
    assert "| Orders › cancels it | specs/orders.highbar.yaml | 0.25s |" in text


def test_hb19_failures_and_errors_come_first_with_why(tmp_path: Path) -> None:
    text = _run(
        tmp_path,
        _outcome("looks the order up", Status.PASSED),
        _outcome("cancels it", Status.FAILED, "Response does not contain 'cancelled'", "two"),
        _outcome("refunds it", Status.ERROR, "refund_order: not observed"),
        code=1,
    )

    assert text.startswith("## highbar: not passed (exit code 1)\n")
    assert re.search(r"^\*\*1 passed\*\* · 1 failed · 1 error · \d+\.\d\ds$", text, re.MULTILINE)
    failed, errored, passed = _rows(text)
    assert failed.startswith("| ✗ failed | Orders › cancels it |")
    assert "does not contain 'cancelled' (and 1 more) |" in failed
    assert errored.startswith("| ! error | Orders › refunds it |")
    assert text.index("✗ failed") < text.index("<details>")
    assert passed.startswith("| Orders › looks the order up |")


def test_hb19_text_from_the_agent_stays_text(tmp_path: Path) -> None:
    """An error quotes the agent: a pipe, a newline or markup must not reshape the page."""
    error = "said: a | b\n<script>x</script> **bold** [link](http://e.test)"

    text = _run(tmp_path, _outcome("cancels it", Status.FAILED, error), code=1)

    (row,) = _rows(text)
    assert row.count(" | ") == 4  # five cells, the error's pipe escaped
    assert "a \\| b<br>" in row
    assert "<script>" not in text
    assert "\\<script\\>" in row
    assert "\\*\\*bold\\*\\*" in row
    assert "\\[link\\](http://e.test)" in row


def test_hb19_a_stopped_run_says_why(tmp_path: Path) -> None:
    report = MarkdownReport(tmp_path / "summary.md", version="0.2.0")
    report.start()
    report.add(
        "specs/orders.highbar.yaml", SPEC, _summary(_outcome("a", Status.PASSED)), complete=False
    )
    report.stop("budget_exceeded", "the run cost 0.12 USD, over --max-cost 0.10")
    report.finish(3)

    text = report.path.read_text(encoding="utf-8")
    assert text.startswith("## highbar: not passed (exit code 3)\n")
    assert "> budget\\_exceeded: the run cost 0.12 USD, over --max-cost 0.10 (exit code 3)" in text


def test_hb19_an_exit_code_with_nothing_failed_is_not_passed(tmp_path: Path) -> None:
    """Every test passed and the run still exited 1 (a late call, a lost trace...)."""
    text = _run(tmp_path, _outcome("a", Status.PASSED), code=1)

    assert text.startswith("## highbar: not passed (exit code 1)\n")
    assert "> the run exited with code 1" in text


def test_hb19_a_long_list_is_cut_and_says_how_much(tmp_path: Path) -> None:
    """GitHub drops a step summary over 1 MiB whole: the page stays under it."""
    tests = [_outcome(f"t{i}", Status.FAILED, "x" * 5000) for i in range(ROW_LIMIT + 7)]

    text = _run(tmp_path, *tests, code=1)

    assert len(_rows(text)) == ROW_LIMIT
    assert "…and 7 more not shown." in text
    assert len(text.encode("utf-8")) < 1024 * 1024


def test_hb19_the_time_is_the_runs_as_the_terminal_shows_it() -> None:
    """The real run said 4.54s in the terminal and 3.05s here: the sum of its tests."""
    test = {"suite": "O", "name": "a", "status": "passed", "errors": [], "duration_ms": 3054.0}
    body = {
        "exit_code": 0,
        "stopped": None,
        "duration_ms": 4577.0,
        "summary": {"total": 1, "passed": 1, "failed": 0, "errored": 0, "variations": 0},
        "specs": [{"key": "s.highbar.yaml", "tests": [test]}],
    }

    assert "**1 passed** · 4.58s" in render(body)
