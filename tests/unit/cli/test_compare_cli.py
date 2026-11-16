"""`highbar compare` from the command line (HB-5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from highbar.cli.main import app

runner = CliRunner()


def _write(path: Path, *tests: tuple[str, int, int]) -> Path:
    body: dict[str, Any] = {
        "schema_version": 1,
        # Each run has its own start, as real reports do: a copy is the same round.
        "started_at": str(path),
        "specs": [
            {
                "key": "specs/sink.highbar.yaml",
                "tests": [
                    {
                        "id": test_id,
                        "definition_sha256": "d1",
                        "counts": {"passed": passed, "failed": failed, "error": 0},
                        "duration_ms": 900.0 * (passed + failed),
                        "totals": {"input_tokens": 50 * (passed + failed), "cost": {"USD": 0.01}},
                    }
                    for test_id, passed, failed in tests
                ],
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return path


def _rounds(folder: Path, n: int, *tests: tuple[str, int, int]) -> Path:
    for i in range(n):
        _write(folder / f"round{i}.json", *tests)
    return folder


def test_his_warranty_case_exits_0_with_no_verdict(tmp_path: Path) -> None:
    base = _write(tmp_path / "a.json", ("warranty", 1, 4))
    head = _write(tmp_path / "b.json", ("warranty", 5, 0))
    result = runner.invoke(
        app, ["compare", "--base", str(base), "--head", str(head), "--fail-on-regression"]
    )
    assert result.exit_code == 0, result.output
    assert "warranty" in result.output
    assert "1/5" in result.output and "5/5" in result.output
    assert "fewer than 3 rounds on a side" in result.output


def test_a_real_regression_fails_the_gate(tmp_path: Path) -> None:
    base = _rounds(tmp_path / "off", 4, ("warranty", 10, 0))
    head = _rounds(tmp_path / "on", 4, ("warranty", 1, 9))
    args = ["compare", "--base", str(base), "--head", str(head)]
    assert runner.invoke(app, args).exit_code == 0
    result = runner.invoke(app, [*args, "--fail-on-regression"])
    assert result.exit_code == 1, result.output
    assert "worse" in result.output


def test_sides_take_files_and_folders_repeated(tmp_path: Path) -> None:
    a = _write(tmp_path / "a1.json", ("warranty", 10, 0))
    b = _rounds(tmp_path / "more", 2, ("warranty", 10, 0))
    head = _rounds(tmp_path / "on", 3, ("warranty", 1, 9))
    result = runner.invoke(
        app, ["compare", "--base", str(a), "--base", str(b), "--head", str(head)]
    )
    assert result.exit_code == 0, result.output
    assert "30/30" in result.output and "3/30" in result.output
    assert "worse" in result.output


def test_json_report_has_the_rows(tmp_path: Path) -> None:
    base = _rounds(tmp_path / "off", 3, ("warranty", 10, 0))
    head = _rounds(tmp_path / "on", 3, ("warranty", 1, 9))
    out = tmp_path / "compare.json"
    result = runner.invoke(
        app, ["compare", "--base", str(base), "--head", str(head), "--report", f"json={out}"]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(out.read_text())["tests"]
    assert rows[0]["test"] == "warranty"
    assert rows[0]["verdict"] == "worse"
    assert rows[0]["base"]["passed"] == 30
    # Review note (c): "executions" holds every one, errors included; no "judged" beside "error".
    assert rows[0]["base"]["executions"] == 30
    assert "judged" not in rows[0]["base"]
    assert rows[0]["base"]["rounds"] == [{"passed": 10, "failed": 0, "error": 0}] * 3
    assert rows[0]["head"]["mean_cost"] == {"USD": 0.001}


def test_an_unreadable_report_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    head = _write(tmp_path / "b.json", ("warranty", 5, 0))
    result = runner.invoke(app, ["compare", "--base", str(bad), "--head", str(head)])
    assert result.exit_code == 2
    assert "bad.json" in result.output


def test_an_empty_folder_exits_2(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    head = _write(tmp_path / "b.json", ("warranty", 5, 0))
    result = runner.invoke(app, ["compare", "--base", str(tmp_path / "empty"), "--head", str(head)])
    assert result.exit_code == 2
    assert "no JSON report" in result.output


def test_nothing_in_common_exits_2(tmp_path: Path) -> None:
    base = _write(tmp_path / "a.json", ("a", 5, 0))
    head = _write(tmp_path / "b.json", ("b", 5, 0))
    result = runner.invoke(app, ["compare", "--base", str(base), "--head", str(head)])
    assert result.exit_code == 2
    assert "no test in common" in result.output


def test_tests_that_did_not_move_take_one_line(tmp_path: Path) -> None:
    base = _write(tmp_path / "a.json", ("warranty", 1, 4), ("menu", 5, 0))
    head = _write(tmp_path / "b.json", ("warranty", 5, 0), ("menu", 5, 0))
    result = runner.invoke(app, ["compare", "--base", str(base), "--head", str(head)])
    lines = result.output.splitlines()
    menu = next(i for i, line in enumerate(lines) if "menu" in line)
    # No reason and no means under a test where nothing moved.
    assert "warranty" in lines[menu + 1] or not lines[menu + 1].startswith("      ")
    # The one-round advice is said once, not once per test.
    assert result.output.count("4 rounds of 5 rather than 1 of 20") == 1
    assert "p 0.0476" in result.output


def test_review_the_same_report_twice_is_refused(tmp_path: Path) -> None:
    # Review of #69: a report given twice, or a folder and a file in it, counted as two rounds.
    base = _rounds(tmp_path / "off", 1, ("warranty", 10, 0))
    head = _write(tmp_path / "on.json", ("warranty", 1, 9))
    for args in (
        ["--base", str(base / "round0.json"), "--base", str(base / "round0.json")],
        ["--base", str(base), "--base", str(base / "round0.json")],
    ):
        result = runner.invoke(app, ["compare", *args, "--head", str(head), "--head", str(head)])
        assert result.exit_code == 2, result.output
        assert "the same report" in result.output


def test_review_a_copy_of_a_report_is_the_same_round(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", ("warranty", 10, 0))
    copy = tmp_path / "copy.json"
    copy.write_bytes(a.read_bytes())
    head = _rounds(tmp_path / "on", 2, ("warranty", 1, 9))
    result = runner.invoke(
        app, ["compare", "--base", str(a), "--base", str(copy), "--head", str(head)]
    )
    assert result.exit_code == 2, result.output
    assert "the same report" in result.output


def test_review_a_reformatted_copy_is_the_same_round(tmp_path: Path) -> None:
    # Review of #69 at 7976aea, note (a): a copy written again with indent=2 counted twice.
    a = _write(tmp_path / "a.json", ("warranty", 10, 0))
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps(json.loads(a.read_text()), indent=2))
    head = _rounds(tmp_path / "on", 2, ("warranty", 1, 9))
    result = runner.invoke(
        app, ["compare", "--base", str(a), "--base", str(copy), "--head", str(head)]
    )
    assert result.exit_code == 2, result.output
    assert "the same report" in result.output


def test_review_why_an_erroring_base_is_not_beaten_is_shown(tmp_path: Path) -> None:
    base = tmp_path / "down"
    for i in range(4):
        _write(base / f"r{i}.json", ("warranty", 0, 0))
        body = json.loads((base / f"r{i}.json").read_text())
        body["specs"][0]["tests"][0]["counts"] = {"passed": 0, "failed": 0, "error": 10}
        (base / f"r{i}.json").write_text(json.dumps(body))
    head = _rounds(tmp_path / "up", 4, ("warranty", 10, 0))
    result = runner.invoke(app, ["compare", "--base", str(base), "--head", str(head)])
    assert result.exit_code == 0, result.output
    assert "rests on the 40 error executions on base" in result.output


def test_hb45_the_line_counts_only_rounds_that_measured(tmp_path: Path) -> None:
    _write(tmp_path / "base" / "r0.json", ("warranty", 10, 0))
    for i in (1, 2):
        _write(tmp_path / "base" / f"r{i}.json", ("warranty", 0, 0))
    head = _rounds(tmp_path / "head", 3, ("warranty", 3, 7))
    result = runner.invoke(app, ["compare", "--base", str(tmp_path / "base"), "--head", str(head)])
    assert "base 10/10 in 1 round " in result.output, result.output
