"""HB-17 end to end: --suite and -k with what develop added after them.

`--max-cost` names "the rest" only when a picked test was left, under `--jobs 1` and
`--jobs 2`; a spec with no test picked starts no agent; and the report's `complete` is
about the tests picked. Each agent is the fixture server, and each turn costs 0.01 USD.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_report import SERVER, _free_port

REST = "raise the ceiling to run the rest"


def _spec(suites: dict[str, list[str]]) -> str:
    port = _free_port()
    start = " ".join([sys.executable, str(SERVER), "--port", str(port), "--cost", "0.01"])
    lines = [
        "agent:",
        f'  start: "{start}"',
        f'  endpoint: "http://127.0.0.1:{port}/chat"',
        f'  health: "http://127.0.0.1:{port}/health"',
        "  startup_timeout: 5000",
        "  settle_time: 50",
        "suites:",
    ]
    for suite, tests in suites.items():
        lines += [f"  - name: {suite}", "    tests:"]
        for test in tests:
            lines += [f'      - trigger: "{test}"', '        expect: {response: {contains: "ok"}}']
    return "\n".join(lines) + "\n"


@dataclass
class _Run:
    code: int
    output: str
    report: dict[str, Any]
    root: Path

    @property
    def ran(self) -> list[str]:
        return [test["id"] for spec in self.report["specs"] for test in spec["tests"]]

    @property
    def message(self) -> str:
        stopped = self.report["stopped"]
        return "" if stopped is None else stopped["message"]

    def started(self, stem: str) -> bool:
        """Whether the agent of the spec ``stem`` started: highbar opens its log first."""
        return any((self.root / ".highbar" / "logs").glob(f"{stem}-*.log"))


def _run(root: Path, *args: str) -> _Run:
    report = root / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", *args, "--report", f"json={report}"],
        cwd=root,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    return _Run(result.returncode, result.stdout + result.stderr, data, root)


@pytest.fixture
def two_specs(tmp_path: Path) -> Path:
    """specs/s1 and specs/s2, two tests each, in one suite named S."""
    specs = tmp_path / "specs"
    specs.mkdir()
    for n in (1, 2):
        tests = [f"spec{n} test1", f"spec{n} test2"]
        (specs / f"s{n}.highbar.yaml").write_text(_spec({"S": tests}), encoding="utf-8")
    return tmp_path


@pytest.fixture
def three_tests(tmp_path: Path) -> Path:
    tests = ["spec3 test1", "spec3 test2", "spec3 test3"]
    (tmp_path / "s3.highbar.yaml").write_text(_spec({"S": tests}), encoding="utf-8")
    return tmp_path


@pytest.fixture
def two_suites(tmp_path: Path) -> Path:
    suites = {"A": ["a test1", "a test2"], "B": ["b test1"]}
    (tmp_path / "s4.highbar.yaml").write_text(_spec(suites), encoding="utf-8")
    return tmp_path


# The cases of the review of 3c16f42 (K0-K7), on develop with --jobs.


@pytest.mark.timeout(60)
@pytest.mark.parametrize("jobs", ["1", "2"])
def test_hb17_k0_no_selection_leaves_spec2_as_the_rest(two_specs: Path, jobs: str) -> None:
    run = _run(two_specs, "specs", "--max-cost", "0.015", "--jobs", jobs)

    assert run.code == 3, run.output
    assert run.ran == ["S/spec1 test1", "S/spec1 test2"]
    assert run.report["complete"] is False
    assert run.report["stopped"]["reason"] == "budget_exceeded"
    assert REST in run.message
    assert not run.started("s2")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("jobs", ["1", "2"])
def test_hb17_k1_a_spec_cut_whole_is_not_the_rest(two_specs: Path, jobs: str) -> None:
    run = _run(two_specs, "specs", "-k", "spec1", "--max-cost", "0.015", "--jobs", jobs)

    assert run.code == 3, run.output
    assert run.ran == ["S/spec1 test1", "S/spec1 test2"]
    assert run.report["complete"] is True
    # With --jobs 2 both tests are in flight, and the one that ends last is named.
    assert "over its ceiling, by the end of its last test" in run.message
    assert REST not in run.message
    assert run.started("s1") and not run.started("s2")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("jobs", ["1", "2"])
def test_hb17_k2_a_picked_test_in_the_next_spec_is_the_rest(two_specs: Path, jobs: str) -> None:
    run = _run(two_specs, "specs", "-k", "test1", "--max-cost", "0.005", "--jobs", jobs)

    assert run.code == 3, run.output
    assert run.ran == ["S/spec1 test1"]
    assert run.report["complete"] is False
    assert run.message.endswith(f"by the end of 'spec1 test1', so it stopped there; {REST}")
    assert not run.started("s2")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("jobs", ["1", "2"])
def test_hb17_k3_over_at_the_last_picked_test_leaves_no_rest(two_specs: Path, jobs: str) -> None:
    run = _run(two_specs, "specs", "-k", "test1", "--max-cost", "0.015", "--jobs", jobs)

    assert run.code == 3, run.output
    assert run.ran == ["S/spec1 test1", "S/spec2 test1"]
    assert run.report["complete"] is True
    assert run.message.endswith("over its ceiling, by the end of its last test, 'spec2 test1'")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("jobs", ["1", "2"])
def test_hb17_k4_a_test_cut_at_the_end_of_a_spec_is_not_the_rest(
    three_tests: Path, jobs: str
) -> None:
    run = _run(
        three_tests, "s3.highbar.yaml", "-k", "test1", "-k", "test2", "--max-cost", "0.015",
        "--jobs", jobs,
    )  # fmt: skip

    assert run.code == 3, run.output
    assert run.ran == ["S/spec3 test1", "S/spec3 test2"]
    assert run.report["complete"] is True
    assert "over its ceiling, by the end of its last test" in run.message
    assert REST not in run.message


@pytest.mark.timeout(60)
def test_hb17_k5_a_picked_test_after_a_cut_one_is_the_rest(three_tests: Path) -> None:
    run = _run(three_tests, "s3.highbar.yaml", "-k", "test1", "-k", "test3", "--max-cost", "0.005")

    assert run.code == 3, run.output
    assert run.ran == ["S/spec3 test1"]
    assert run.report["complete"] is False
    assert REST in run.message


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("suite", "ceiling", "ran"),
    [("A", "0.015", ["A/a test1", "A/a test2"]), ("B", "0.005", ["B/b test1"])],
)
def test_hb17_k6_k7_a_suite_cut_is_not_the_rest(
    two_suites: Path, suite: str, ceiling: str, ran: list[str]
) -> None:
    run = _run(two_suites, "s4.highbar.yaml", "--suite", suite, "--max-cost", ceiling)

    assert run.code == 3, run.output
    assert run.ran == ran
    assert run.report["complete"] is True
    assert run.message.endswith(f"by the end of its last test, '{ran[-1].split('/')[1]}'")


# Without a ceiling.


@pytest.mark.timeout(60)
def test_hb17_jobs_2_runs_only_the_picked_tests_in_spec_order(tmp_path: Path) -> None:
    tests = ["t1 alpha", "t2 beta", "t3 alpha", "t4 alpha", "t5 beta"]
    (tmp_path / "s5.highbar.yaml").write_text(_spec({"S": tests}), encoding="utf-8")

    run = _run(tmp_path, "s5.highbar.yaml", "-k", "alpha", "--jobs", "2")

    assert run.code == 0, run.output
    assert run.ran == ["S/t1 alpha", "S/t3 alpha", "S/t4 alpha"]
    shown = [run.output.index(name) for name in ("t1 alpha", "t3 alpha", "t4 alpha")]
    assert shown == sorted(shown), run.output
    assert "beta" not in run.output
    assert run.report["complete"] is True


@pytest.mark.timeout(60)
@pytest.mark.parametrize("jobs", ["1", "2"])
def test_hb17_every_picked_test_ran_is_complete(two_specs: Path, jobs: str) -> None:
    run = _run(two_specs, "specs", "-k", "spec2 test2", "--jobs", jobs)

    assert run.code == 0, run.output
    assert run.ran == ["S/spec2 test2"]
    assert [spec["path"] for spec in run.report["specs"]] == ["specs/s2.highbar.yaml"]
    assert run.report["complete"] is True
    assert run.started("s2") and not run.started("s1")
