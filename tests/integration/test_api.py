"""highbar.run(): running a spec from Python, against the stand-in server."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

import highbar
from highbar.engine.schema import SpecError

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.timeout(30)
def test_run_by_code_returns_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    port = _free_port()
    spec = tmp_path / "ok.highbar.yaml"
    spec.write_text(
        f"""\
agent:
  start: "{sys.executable} {SERVER} --port {port}"
  endpoint: "http://127.0.0.1:{port}/chat"
  health: "http://127.0.0.1:{port}/health"
  settle_time: 50
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect:
          response: {{contains: "ok"}}
""",
        encoding="utf-8",
    )

    summary = highbar.run(spec)

    assert (summary.total, summary.passed) == (1, 1)


@pytest.mark.timeout(30)
@pytest.mark.parametrize("replies, passed", [("ok,ok,no", 1), ("ok,no,no", 0)])
def test_run_by_code_uses_defaults_min_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: str, passed: int
) -> None:
    """2 of 3 runs reach defaults.min_pass 0.66; 1 of 3 does not."""
    monkeypatch.chdir(tmp_path)
    port = _free_port()
    spec = tmp_path / "flaky.highbar.yaml"
    spec.write_text(
        f"""\
agent:
  start: "{sys.executable} {SERVER} --port {port} --replies {replies}"
  endpoint: "http://127.0.0.1:{port}/chat"
  health: "http://127.0.0.1:{port}/health"
  settle_time: 50
defaults: {{runs: 3, min_pass: 0.66}}
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect:
          response: {{contains: "ok"}}
""",
        encoding="utf-8",
    )

    summary = highbar.run(spec)

    assert (summary.total, summary.passed, summary.failed) == (1, passed, 1 - passed)


def test_run_by_code_refuses_defaults_min_pass_above_runs(tmp_path: Path) -> None:
    spec = tmp_path / "strict.highbar.yaml"
    spec.write_text(
        """\
agent:
  start: "true"
  endpoint: "http://127.0.0.1:9/chat"
defaults: {min_pass: 3}
suites:
  - name: S
    tests:
      - {trigger: "a", expect: {response: {contains: "ok"}}}
""",
        encoding="utf-8",
    )

    with pytest.raises(
        SpecError, match=r"suites\[0\]\.tests\[0\]: min_pass 3 from defaults\.min_pass .* 2 runs"
    ):
        highbar.run(spec, runs=2)


def test_run_by_code_raises_spec_error(tmp_path: Path) -> None:
    spec = tmp_path / "bad.highbar.yaml"
    spec.write_text('agent:\n  start: "true"\n', encoding="utf-8")

    with pytest.raises(SpecError, match="missing 'endpoint'"):
        highbar.run(spec)
