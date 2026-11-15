"""HB-19: several formats of one run's report, each in its own file."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from highbar.domain.models import Status
from highbar.reporters.json_report import JsonReport
from highbar.reporters.junit_report import JUnitReport
from highbar.reporters.markdown_report import MarkdownReport
from highbar.reporters.reports import Reports
from tests.unit.reporters.test_markdown_report import SPEC, _outcome, _summary


def _reports(tmp_path: Path) -> Reports:
    parts: list[JsonReport] = [
        JsonReport(tmp_path / "report.json", version="0.2.0"),
        JUnitReport(tmp_path / "junit.xml", version="0.2.0"),
        MarkdownReport(tmp_path / "summary.md", version="0.2.0"),
    ]
    return Reports(parts, version="0.2.0")


def test_hb19_every_file_says_the_run_did_not_finish_from_the_start(tmp_path: Path) -> None:
    reports = _reports(tmp_path)

    reports.start()

    assert reports.paths == [
        tmp_path / "report.json",
        tmp_path / "junit.xml",
        tmp_path / "summary.md",
    ]
    assert json.loads((tmp_path / "report.json").read_text())["exit_code"] is None
    assert ET.parse(tmp_path / "junit.xml").getroot().get("errors") == "1"
    assert (tmp_path / "summary.md").read_text().startswith("## highbar: the run did not finish")


def test_hb19_one_run_ends_in_every_file(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    reports.start()
    reports.add("specs/orders.highbar.yaml", SPEC, _summary(_outcome("a", Status.PASSED)), True)

    reports.finish(0)

    report = json.loads((tmp_path / "report.json").read_text())
    assert (report["exit_code"], report["complete"], report["summary"]["passed"]) == (0, True, 1)
    assert ET.parse(tmp_path / "junit.xml").getroot().get("tests") == "1"
    assert (tmp_path / "summary.md").read_text().startswith("## highbar: passed\n")


def test_hb19_one_report_has_one_path(tmp_path: Path) -> None:
    assert JsonReport(tmp_path / "r.json", version="0.2.0").paths == [tmp_path / "r.json"]
