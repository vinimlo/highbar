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
    assert "one round on a side" in result.output


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
    b = _write(tmp_path / "a2.json", ("warranty", 10, 0))
    head = _rounds(tmp_path / "on", 2, ("warranty", 1, 9))
    result = runner.invoke(
        app, ["compare", "--base", str(a), "--base", str(b), "--head", str(head)]
    )
    assert result.exit_code == 0, result.output
    assert "20/20" in result.output and "2/20" in result.output
    assert "worse" in result.output


def test_json_report_has_the_rows(tmp_path: Path) -> None:
    base = _rounds(tmp_path / "off", 2, ("warranty", 10, 0))
    head = _rounds(tmp_path / "on", 2, ("warranty", 1, 9))
    out = tmp_path / "compare.json"
    result = runner.invoke(
        app, ["compare", "--base", str(base), "--head", str(head), "--report", f"json={out}"]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(out.read_text())["tests"]
    assert rows[0]["test"] == "warranty"
    assert rows[0]["verdict"] == "worse"
    assert rows[0]["base"]["passed"] == 20
    assert rows[0]["base"]["rounds"] == [{"passed": 10, "failed": 0, "error": 0}] * 2
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
