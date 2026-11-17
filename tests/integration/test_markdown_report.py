"""HB-19: `highbar run --report markdown=<file>`, and more than one --report, with the real CLI."""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET

import pytest

from tests.integration.test_report import FAILS, PASSES, _Project
from tests.integration.test_report import project as project  # noqa: F401 (fixture)


def _run(project: _Project, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        project.command(project.spec(PASSES, FAILS), *args),
        cwd=project.root,
        env=project.env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.timeout(30)
def test_hb19_a_run_writes_a_markdown_summary(project: _Project) -> None:
    summary = project.root / "summary.md"

    result = _run(project, "--report", f"markdown={summary}")

    assert result.returncode == 1, result.stdout + result.stderr
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("## highbar: not passed (exit code 1)\n")
    assert "| ✗ failed |" in text
    assert f"Report: {summary}" in result.stdout


@pytest.mark.timeout(30)
def test_hb19_one_run_writes_each_report_asked_for(project: _Project) -> None:
    junit, summary, report = (project.root / n for n in ("junit.xml", "summary.md", "r.json"))

    result = _run(
        project,
        *("--report", f"junit={junit}", "--report", f"markdown={summary}"),
        *("--report", f"json={report}"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert ET.parse(junit).getroot().get("failures") == "1"
    assert summary.read_text(encoding="utf-8").startswith("## highbar: not passed")
    assert json.loads(report.read_text())["exit_code"] == 1
    assert f"Report: {junit}, {summary}, {report}" in result.stdout


@pytest.mark.timeout(30)
def test_hb19_the_same_file_twice_is_refused_before_anything_runs(project: _Project) -> None:
    target = project.root / "out"

    result = _run(project, "--report", f"json={target}", "--report", f"junit={target}")

    assert result.returncode == 2
    assert f"--report: {target} is given twice" in result.stdout
    assert not target.exists()
