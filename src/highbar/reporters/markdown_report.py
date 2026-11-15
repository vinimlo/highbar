"""The report of a run as Markdown (HB-19), for a CI job's summary page.

GitHub shows the file at ``$GITHUB_STEP_SUMMARY`` on the page of the run. It is the
JSON report in another format, with the same lifecycle: replaced before the first
message, written on every way out, written at once. The heading says whether the run
passed, the tests that did not pass come first with why, and the ones that passed are
folded under them.

Nothing written by the agent can reshape the page: every piece of text is escaped, so
a pipe stays in its cell and a tag or a link is shown as written. GitHub drops a step
summary over 1 MiB whole, so a long list is cut and says how much was left out.
"""

from __future__ import annotations

import re
from typing import Any

from highbar.reporters._run import printable, run_error
from highbar.reporters.json_report import JsonReport

# Rows per table, and characters per cell before the escapes: with both, the page stays
# well under GitHub's 1 MiB for a step summary, whatever the agent wrote.
ROW_LIMIT = 200
CELL_LIMIT = 300

# What GitHub's Markdown reads as markup in the middle of a line: emphasis, code, links,
# HTML and entities, a table's pipe, strikethrough and math. A backslash makes each one
# text. Nothing the agent wrote starts a line (a newline becomes <br>, and a blockquote
# starts with highbar's own words), so the marks that only count there are left alone.
_MARKUP = re.compile(r"([\\`*_\[\]<>|~&$])")
_MARKS = {"failed": "✗ failed", "error": "! error"}


class MarkdownReport(JsonReport):
    """The run's report as a Markdown summary."""

    def _render(self, body: dict[str, Any]) -> bytes:
        return render(body).encode("utf-8", "backslashreplace")


def render(body: dict[str, Any]) -> str:
    rows = [(spec["key"], test) for spec in body["specs"] for test in spec["tests"]]
    not_passed = [(key, test) for key, test in rows if test["status"] != "passed"]
    passed = [(key, test) for key, test in rows if test["status"] == "passed"]
    lines = [f"## highbar: {_verdict(body['exit_code'])}", ""]
    reason = run_error(body, failed=bool(not_passed))
    if reason is not None:
        lines += [f"> {_cell(reason)}", ""]
    lines += [_counts(body), ""]
    if not_passed:
        lines += ["| Result | Test | Spec | Time | Why |", "| --- | --- | --- | --- | --- |"]
        lines += [
            f"| {_MARKS.get(str(test['status']), str(test['status']))} | {_name(key, test)} "
            f"| {_cell(key)} | {_seconds(test)} | {_why(test)} |"
            for key, test in not_passed[:ROW_LIMIT]
        ]
        lines += _left_out(len(not_passed))
        lines.append("")
    if passed:
        lines += [f"<details><summary>{len(passed)} passed</summary>", ""]
        lines += ["| Test | Spec | Time |", "| --- | --- | --- |"]
        lines += [
            f"| {_name(key, test)} | {_cell(key)} | {_seconds(test)} |"
            for key, test in passed[:ROW_LIMIT]
        ]
        lines += _left_out(len(passed))
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def _verdict(code: int | None) -> str:
    if code is None:
        return "the run did not finish"
    if code == 0:
        return "passed"
    return f"not passed (exit code {code})"


def _counts(body: dict[str, Any]) -> str:
    """``**N passed** · N failed · N error · N variations · 1.23s``, without zero counts."""
    summary = body["summary"]
    parts = [f"**{summary['passed']} passed**"]
    if summary["failed"]:
        parts.append(f"{summary['failed']} failed")
    if summary["errored"]:
        parts.append(f"{summary['errored']} error")
    if summary["variations"]:
        parts.append(f"{summary['variations']} variations")
    tests_ms = sum(test.get("duration_ms") or 0 for spec in body["specs"] for test in spec["tests"])
    parts.append(f"{tests_ms / 1000:.2f}s")
    return " · ".join(parts)


def _name(key: str, test: dict[str, Any]) -> str:
    return f"{_cell(test['suite'] or key)} › {_cell(test['name'])}"


def _why(test: dict[str, Any]) -> str:
    errors = list(test["errors"]) or [str(test["status"])]
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return _cell(errors[0]) + more


def _seconds(test: dict[str, Any]) -> str:
    return f"{(test.get('duration_ms') or 0) / 1000:.2f}s"


def _left_out(count: int) -> list[str]:
    return ["", f"…and {count - ROW_LIMIT} more not shown."] if count > ROW_LIMIT else []


def _cell(value: Any) -> str:
    """Text as it is, in one table cell: escaped, on one line, cut when it is long."""
    text = printable(value)
    if len(text) > CELL_LIMIT:
        text = text[:CELL_LIMIT] + "…"
    text = _MARKUP.sub(r"\\\1", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
