"""The examples in examples/ are specs that validate and agents that compile.

The quickstart also runs end to end (tests/integration/test_quickstart.py). The
OpenAI-compatible agent needs a model, so CI only checks that it is well formed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
NAMES = ["quickstart", "openai-agent", "trace-contract"]


@pytest.mark.parametrize("name", NAMES)
def test_example_specs_validate(name: str) -> None:
    specs = sorted((EXAMPLES / name / "specs").glob("*.highbar.yaml"))
    assert specs, f"examples/{name} has no spec"
    for spec in specs:
        parse_spec(str(spec))


@pytest.mark.parametrize("name", NAMES)
def test_example_agents_compile(name: str) -> None:
    agent = EXAMPLES / name / "agent.py"
    compile(agent.read_text(encoding="utf-8"), str(agent), "exec")
    assert (EXAMPLES / name / "requirements.txt").is_file()
    assert (EXAMPLES / name / "README.md").is_file()
