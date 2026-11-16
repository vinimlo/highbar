"""An agent that writes the trace contract itself, without the highbar package (D7).

The reference emitter on the trace contract page is examples/trace-contract/emitter.py;
this runs an agent that uses it, started by highbar (PR 8 review, I2).
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from tests.unit.reporters.test_json_report import _closed, _schema

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "trace-contract"


@pytest.mark.timeout(120)
def test_i2_an_agent_with_the_reference_emitter_runs_green() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run"],
        cwd=EXAMPLE,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


@pytest.mark.timeout(120)
def test_ac14_the_reference_emitter_writes_usage_and_the_report_shows_it(tmp_path: Path) -> None:
    report = tmp_path / "report.json"

    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", "--report", f"json={report}"],
        cwd=EXAMPLE,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "USD" in result.stdout
    data = json.loads(report.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(_closed(_schema())).validate(data)
    (spec,) = data["specs"]
    orders = spec["tests"][0]
    assert orders["budget"] == {"max_input_tokens": 100, "max_cost": None}
    (turn,) = orders["phrasings"][0]["executions"][0]["turns"]
    assert turn["usage"]["model"] == "gpt-4o-mini"
    assert turn["usage"]["input_tokens"] == 6  # the words of its trigger
    assert turn["usage"]["currency"] == "USD"  # the agent sent no cost: it is the table's
    assert turn["usage"]["cost"] > 0
    assert data["totals"]["complete"] is True
    assert [suite["name"] for suite in spec["suites"]] == ["Orders", "Guardrails"]


def test_a_message_that_breaks_the_handler_still_writes_its_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`emitter.turn` promises it: "also when the handling failed"."""
    traces = tmp_path / "traces.jsonl"
    monkeypatch.setenv("HIGHBAR_TRACES_URL", str(traces))
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.delitem(sys.modules, "emitter", raising=False)
    spec = importlib.util.spec_from_file_location("trace_contract_agent", EXAMPLE / "agent.py")
    assert spec is not None
    assert spec.loader is not None
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    handler = agent.Handler.__new__(agent.Handler)
    handler.headers = {"X-Highbar-Test": "t1", "Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")

    with pytest.raises(KeyError):
        handler.do_POST()

    (line,) = traces.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert (record["type"], record["context"]) == ("turn", {"test": "t1"})
    assert record["usage"]["input_tokens"] == 0


def test_i2_the_example_imports_no_highbar_and_the_emitter_fits_in_40_lines() -> None:
    for name in ("emitter.py", "agent.py"):
        tree = ast.parse((EXAMPLE / name).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported |= {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert not {m for m in imported if m.split(".")[0] == "highbar"}, name
    assert len((EXAMPLE / "emitter.py").read_text(encoding="utf-8").splitlines()) <= 40
