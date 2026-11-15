"""HB-19: several formats of one run's report, each in its own file."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from highbar.domain.models import Status
from highbar.reporters.json_report import JsonReport, ReportError
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


def _unfinished(path: Path) -> bool:
    """Whether the file says the run did not finish, in its own format."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)["exit_code"] is None
    if path.suffix == ".xml":
        return ET.fromstring(text).get("errors") == "1"
    return text.startswith("## highbar: the run did not finish\n")


@pytest.mark.parametrize("first", ["json", "markdown"])
def test_hb19_a_later_file_that_cannot_be_written_leaves_the_earlier_one_unfinished(
    tmp_path: Path, first: str
) -> None:
    """The run exits 2 when a report cannot be written: no file may say it passed."""
    json_part = JsonReport(tmp_path / "out" / "r.json", version="0.2.0")
    page = MarkdownReport(tmp_path / "page" / "summary.md", version="0.2.0")
    parts: list[JsonReport] = [json_part, page] if first == "json" else [page, json_part]
    reports = Reports(parts, version="0.2.0")
    reports.start()
    reports.add("specs/orders.highbar.yaml", SPEC, _summary(_outcome("a", Status.PASSED)), True)
    later = parts[1].path.parent
    shutil.rmtree(later)
    later.write_text("a file where the folder was\n")

    with pytest.raises(ReportError):
        reports.finish(0)

    assert _unfinished(parts[0].path)
    assert [p.name for p in parts[0].path.parent.iterdir()] == [parts[0].path.name]


def test_hb19_a_file_that_cannot_be_replaced_puts_back_the_ones_before_it(tmp_path: Path) -> None:
    """Every temporary was written, and the last replace failed: the others go back."""
    reports = _reports(tmp_path)
    reports.start()
    reports.add("specs/orders.highbar.yaml", SPEC, _summary(_outcome("a", Status.PASSED)), True)
    (tmp_path / "summary.md").unlink()
    (tmp_path / "summary.md").mkdir()

    with pytest.raises(ReportError):
        reports.finish(0)

    assert _unfinished(tmp_path / "report.json")
    assert _unfinished(tmp_path / "junit.xml")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["junit.xml", "report.json", "summary.md"]


class _FullAfterTwo(JsonReport):
    """A report whose disk fills up after two writes: start() and finish()."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, version="0.2.0")
        self._writes = 0

    def _stage(self, body: dict[str, Any]) -> str:
        self._writes += 1
        if self._writes > 2:
            raise ReportError(f"cannot write the report to {self.path}: No space left on device")
        return super()._stage(body)


def test_hb19_a_file_that_cannot_be_put_back_is_named_in_the_error(tmp_path: Path) -> None:
    """A double fault: the last replace fails, and so does putting r.json back.

    r.json still holds the final result while the run exits 2: the error must say so.
    """
    first = _FullAfterTwo(tmp_path / "r.json")
    page = MarkdownReport(tmp_path / "summary.md", version="0.2.0")
    reports = Reports([first, page], version="0.2.0")
    reports.start()
    reports.add("specs/orders.highbar.yaml", SPEC, _summary(_outcome("a", Status.PASSED)), True)
    (tmp_path / "summary.md").unlink()
    (tmp_path / "summary.md").mkdir()

    with pytest.raises(ReportError) as raised:
        reports.finish(0)

    message = str(raised.value)
    assert message.startswith(f"cannot write the report to {tmp_path / 'summary.md'}: ")
    assert f"{tmp_path / 'r.json'} could not be put back" in message
    assert json.loads((tmp_path / "r.json").read_text())["exit_code"] == 0  # what it names
