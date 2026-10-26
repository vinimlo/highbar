"""An agent that writes the trace contract itself, without the highbar package (D7).

The reference emitter on the trace contract page is examples/trace-contract/emitter.py;
this runs an agent that uses it, started by highbar (PR 8 review, I2).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
