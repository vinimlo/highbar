"""The report of a run as JUnit XML (HB-19), for the CI tools that read that format.

It is the JSON report in another format, with the same lifecycle: replaced before the
first message, written on every way out, written at once. One ``testsuite`` per spec
and one ``testcase`` per test, with a ``failure`` or an ``error`` that carries the
test's errors.

A JUnit reader judges by the cases it finds, and zero failed cases reads as green. So a
run that did not finish, stopped halfway, or exited with an error code while no test
failed gets one more case, ``highbar.run``, with an ``error`` that says why.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from highbar.reporters._run import printable as _text
from highbar.reporters._run import run_error as _run_error
from highbar.reporters.json_report import JsonReport


class JUnitReport(JsonReport):
    """The run's report as JUnit XML."""

    def _render(self, body: dict[str, Any]) -> bytes:
        root = ET.Element("testsuites", name="highbar")
        totals = {"tests": 0, "failures": 0, "errors": 0}
        for spec in body["specs"]:
            _add_spec(root, spec, totals)
        reason = _run_error(body, failed=totals["failures"] + totals["errors"] > 0)
        if reason is not None:
            suite = ET.SubElement(root, "testsuite", name="highbar", tests="1", failures="0")
            suite.set("errors", "1")
            suite.set("time", _seconds(0))
            case = ET.SubElement(suite, "testcase", classname="highbar", name="run", time="0")
            ET.SubElement(case, "error", message=_text(reason)).text = _text(reason)
            totals["tests"] += 1
            totals["errors"] += 1
        for key, count in totals.items():
            root.set(key, str(count))
        root.set("time", _seconds(body.get("duration_ms")))
        ET.indent(root)
        text = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
        return (text + "\n").encode("utf-8")


def _add_spec(root: ET.Element, spec: dict[str, Any], totals: dict[str, int]) -> None:
    suite = ET.SubElement(root, "testsuite", name=_text(spec["key"]))
    counts = {"tests": 0, "failures": 0, "errors": 0}
    elapsed = 0.0
    for test in spec["tests"]:
        counts["tests"] += 1
        elapsed += test.get("duration_ms") or 0.0
        case = ET.SubElement(
            suite,
            "testcase",
            classname=_text(test["suite"] or spec["key"]),
            name=_text(test["name"]),
            time=_seconds(test.get("duration_ms")),
        )
        status = str(test["status"])
        if status == "passed":
            continue
        tag, count = ("failure", "failures") if status == "failed" else ("error", "errors")
        counts[count] += 1
        errors = [_text(error) for error in test["errors"]] or [status]
        ET.SubElement(case, tag, message=errors[0]).text = "\n".join(errors)
    for key, count in counts.items():
        suite.set(key, str(count))
        totals[key] += count
    suite.set("time", _seconds(elapsed))


def _seconds(milliseconds: Any) -> str:
    return f"{(milliseconds or 0) / 1000:.3f}"
