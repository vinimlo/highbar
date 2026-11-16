"""Review of #74: the reference emitter on the trace contract page wrote "result": null
for every call, a return of null, when the agent gave it no result (HB-37)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

EMITTER = Path(__file__).resolve().parents[2] / "examples" / "trace-contract" / "emitter.py"


def test_review_74_a_call_without_result_has_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("HIGHBAR_TRACES_URL", str(path))
    spec = importlib.util.spec_from_file_location("reference_emitter", EMITTER)
    assert spec is not None and spec.loader is not None
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)

    emitter.tool("search", {"q": "x"}, started_at=1.0)
    emitter.tool("search", {"q": "y"}, result=None, started_at=1.0)

    first, second = (json.loads(line) for line in path.read_text().splitlines())
    assert "result" not in first
    assert second["result"] is None
