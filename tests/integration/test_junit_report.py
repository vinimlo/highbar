"""HB-19: `highbar run --report junit=<file>` with the real CLI."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET

import pytest

from tests.integration.test_report import FAILS, PASSES, _Project
from tests.integration.test_report import project as project  # noqa: F401 (fixture)


@pytest.mark.timeout(30)
def test_hb19_a_run_writes_junit_xml(project: _Project) -> None:
    junit = project.root / "junit.xml"

    result = subprocess.run(
        project.command(project.spec(PASSES, FAILS), "--report", f"junit={junit}"),
        cwd=project.root,
        env=project.env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    root = ET.parse(junit).getroot()
    assert (root.get("tests"), root.get("failures"), root.get("errors")) == ("2", "1", "0")
    assert f"Report: {junit}" in result.stdout


@pytest.mark.timeout(30)
def test_hb19_a_format_it_does_not_know_names_both(project: _Project) -> None:
    result = subprocess.run(
        project.command(project.spec(PASSES), "--report", "xml=out.xml"),
        cwd=project.root,
        env=project.env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert "json=<file>, junit=<file> or markdown=<file>" in result.stdout
