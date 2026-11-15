"""HB-19: the report as JUnit XML, for the CI tools that read it.

It is the JSON report's run, in another format: replaced before the first message,
written on every way out, and never green when the run was not.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from highbar.domain.models import AgentConfig, Counts, RunSummary, Spec, Status, Suite
from highbar.domain.models import Test as Case
from highbar.domain.models import TestOutcome as Outcome
from highbar.reporters.junit_report import JUnitReport

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


def _report(tmp_path: Path) -> JUnitReport:
    return JUnitReport(tmp_path / "junit.xml", version="0.2.0")


def _cases(root: ET.Element) -> list[ET.Element]:
    return root.findall("./testsuite/testcase")


def test_hb19_an_unfinished_run_is_not_green(tmp_path: Path) -> None:
    """A run that dies leaves this file: a reader must not see a green suite."""
    report = _report(tmp_path)

    report.start()

    root = ET.parse(report.path).getroot()
    assert root.tag == "testsuites"
    assert root.get("errors") == "1"
    (case,) = _cases(root)
    error = case.find("error")
    assert error is not None
    assert "did not finish" in (error.get("message") or "")


def test_hb19_each_test_is_a_testcase_with_its_status(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.start()
    summary = _summary(
        _outcome("looks the order up", Status.PASSED),
        _outcome("cancels it", Status.FAILED, "Response does not contain 'cancelled'", "two"),
        _outcome("asks first", Status.ERROR, "HTTP 500 from http://x/chat: boom"),
    )

    report.add("specs/orders.highbar.yaml", SPEC, summary, complete=True)
    report.finish(1)

    root = ET.parse(report.path).getroot()
    (suite,) = root.findall("testsuite")
    assert suite.get("name") == "specs/orders.highbar.yaml"
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == ("3", "1", "1")
    assert (root.get("tests"), root.get("failures"), root.get("errors")) == ("3", "1", "1")
    passed, failed, errored = _cases(root)
    assert (passed.get("classname"), passed.get("name"), passed.get("time")) == (
        "Orders",
        "looks the order up",
        "1.500",
    )
    assert list(passed) == []
    failure = failed.find("failure")
    assert failure is not None
    assert failure.get("message") == "Response does not contain 'cancelled'"
    assert failure.text == "Response does not contain 'cancelled'\ntwo"
    error = errored.find("error")
    assert error is not None
    assert error.get("message") == "HTTP 500 from http://x/chat: boom"


def test_hb19_a_passing_run_has_only_its_tests(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.start()
    report.add("a.highbar.yaml", SPEC, _summary(_outcome("t", Status.PASSED)), complete=True)

    report.finish(0)

    root = ET.parse(report.path).getroot()
    assert (root.get("tests"), root.get("failures"), root.get("errors")) == ("1", "0", "0")
    assert [case.get("name") for case in _cases(root)] == ["t"]


def test_hb19_a_run_stopped_halfway_has_an_error_even_if_what_ran_passed(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.start()
    report.add("a.highbar.yaml", SPEC, _summary(_outcome("t", Status.PASSED)), complete=False)

    report.stop("interrupted", "stopped by Ctrl-C")
    report.finish(130)

    root = ET.parse(report.path).getroot()
    assert root.get("errors") == "1"
    run = _cases(root)[-1]
    assert (run.get("classname"), run.get("name")) == ("highbar", "run")
    error = run.find("error")
    assert error is not None
    assert error.get("message") == "interrupted: stopped by Ctrl-C (exit code 130)"


def test_hb19_a_run_that_could_not_start_is_an_error(tmp_path: Path) -> None:
    """Exit 2 with no test run: zero test cases would read as green."""
    report = _report(tmp_path)
    report.start()

    report.stop("spec_invalid", "a.highbar.yaml: unknown key 'responce'")
    report.finish(2)

    root = ET.parse(report.path).getroot()
    assert (root.get("tests"), root.get("errors")) == ("1", "1")
    error = _cases(root)[0].find("error")
    assert error is not None
    assert "spec_invalid" in (error.get("message") or "")


def test_hb19_a_nonzero_exit_with_nothing_failed_is_an_error(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.start()
    report.add("a.highbar.yaml", SPEC, _summary(_outcome("t", Status.PASSED)), complete=True)

    report.finish(3)

    root = ET.parse(report.path).getroot()
    error = _cases(root)[-1].find("error")
    assert error is not None
    assert error.get("message") == "the run exited with code 3"


def test_hb19_text_xml_cannot_hold_is_escaped_and_the_file_parses(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.start()
    nasty = "got '\x00\x1b[31mred\x1b[0m' & <b>"
    report.add("a.highbar.yaml", SPEC, _summary(_outcome("t", Status.FAILED, nasty)), complete=True)

    report.finish(1)

    failure = _cases(ET.parse(report.path).getroot())[0].find("failure")
    assert failure is not None
    assert failure.get("message") == "got '\\x00\\x1b[31mred\\x1b[0m' & <b>"
