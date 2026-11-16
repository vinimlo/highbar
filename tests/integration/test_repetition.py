"""defaults.min_pass end to end: the real CLI against the stand-in server.

The server answers in turn from --replies, so a test that wants "ok" passes the runs
that get it and fails the others: the minimum decides the result.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(tmp_path: Path, defaults: str, replies: list[str], *args: str) -> tuple[int, str]:
    port = _free_port()
    (tmp_path / "spec.highbar.yaml").write_text(
        f"""\
agent:
  start: "{sys.executable} {SERVER} --port {port} --replies {",".join(replies)}"
  endpoint: "http://127.0.0.1:{port}/chat"
  health: "http://127.0.0.1:{port}/health"
  startup_timeout: 5000
  settle_time: 50
defaults: {defaults}
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect:
          response: {{contains: "ok"}}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", "spec.highbar.yaml", *args],
        cwd=tmp_path,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout + result.stderr


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "replies, code, shown",
    [
        (["ok", "ok", "no"], 0, "✓ hi 2/3 (min 2)"),
        (["ok", "no", "no"], 1, "✗ hi 1/3 (min 2)"),
    ],
)
def test_defaults_min_pass_decides_the_run(
    tmp_path: Path, replies: list[str], code: int, shown: str
) -> None:
    returncode, out = _run(tmp_path, "{runs: 3, min_pass: 0.66}", replies)

    assert returncode == code, out
    assert shown in out


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "replies, code, shown",
    [
        (["ok"] * 7 + ["no"] * 3, 0, "✓ hi 7/10 (min 7)"),
        (["ok"] * 6 + ["no"] * 4, 1, "✗ hi 6/10 (min 7)"),
    ],
)
def test_cli_runs_recomputes_a_fraction_default(
    tmp_path: Path, replies: list[str], code: int, shown: str
) -> None:
    returncode, out = _run(tmp_path, "{runs: 3, min_pass: 0.7}", replies, "--runs", "10")

    assert returncode == code, out
    assert shown in out


@pytest.mark.timeout(60)
def test_cli_runs_keeps_a_whole_number_default(tmp_path: Path) -> None:
    returncode, out = _run(
        tmp_path, "{runs: 3, min_pass: 3}", ["ok", "ok", "ok", "no", "no"], "--runs", "5"
    )

    assert returncode == 0, out
    assert "✓ hi 3/5 (min 3)" in out
