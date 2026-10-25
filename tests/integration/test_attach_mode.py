"""Attach mode end to end: the agent is already running, highbar only talks to it (D5).

Each test starts tests/fixtures/server.py itself, with HIGHBAR_TRACES_URL pointing at
a file in the test's directory, and runs the real CLI against it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from highbar.adapters.ports import accepts_connections

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "server.py"

# Prints a session whose conversation_id is derived from the test id, as a real
# session hook would return the id of the conversation it opened.
SESSION_HOOK = """\
import json, sys
print("opening a conversation...")
print(json.dumps({"conversation_id": int(sys.argv[1][:8], 16)}))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Attached:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _free_port()
        self.traces = root / "var" / "traces.jsonl"
        self.traces.parent.mkdir()
        (root / "session_hook.py").write_text(SESSION_HOOK, encoding="utf-8")
        self.server: subprocess.Popen[bytes] | None = None

    def start(self, *args: str) -> None:
        env = {**os.environ, "HIGHBAR_TRACES_URL": str(self.traces)}
        self.server = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(self.port), *args], env=env
        )
        deadline = time.monotonic() + 10
        while not accepts_connections("127.0.0.1", self.port):
            assert time.monotonic() < deadline, "the stand-in agent did not come up"
            time.sleep(0.05)

    def stop(self) -> None:
        if self.server is not None:
            self.server.kill()
            self.server.wait()

    def spec(self, extra: str = "", expect: str = "function_calls: []") -> str:
        (self.root / "agent.highbar.yaml").write_text(
            f"""\
agent:
  endpoint: "http://127.0.0.1:{self.port}/chat"
  traces: "var/traces.jsonl"
  settle_time: 50
{extra}
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect:
          {expect}
      - trigger: "hello again"
        expect:
          response: {{contains: "ok"}}
""",
            encoding="utf-8",
        )
        return "agent.highbar.yaml"

    def run(self, spec: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "highbar.cli.main", "run", spec],
            cwd=self.root,
            env={**os.environ, "COLUMNS": "200"},
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.fixture
def attached(tmp_path: Path) -> Iterator[_Attached]:
    agent = _Attached(tmp_path)
    try:
        yield agent
    finally:
        agent.stop()


SESSION = f"""\
  session:
    setup: "{sys.executable} session_hook.py {{{{test.id}}}}"
  request:
    body: {{message: "{{{{message}}}}", conversation_id: "{{{{session.conversation_id}}}}"}}
"""


@pytest.mark.timeout(40)
def test_attach_mode_runs_against_a_running_agent(attached: _Attached) -> None:
    attached.start()

    result = attached.run(attached.spec())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    # highbar did not start it, so it does not stop it, nor remove its file.
    assert accepts_connections("127.0.0.1", attached.port)
    assert attached.traces.exists()
    assert "Agent log" not in result.stdout


@pytest.mark.timeout(40)
def test_attach_mode_with_nothing_listening_exits_2(attached: _Attached) -> None:
    result = attached.run(attached.spec())

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"nothing answers at 127.0.0.1:{attached.port}" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.timeout(40)
def test_attach_mode_correlates_by_session(attached: _Attached) -> None:
    attached.start()
    spec = attached.spec(
        SESSION + '  correlate: {conversation_id: "{{session.conversation_id}}"}\n'
    )

    result = attached.run(spec)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


@pytest.mark.timeout(40)
def test_attach_mode_correlate_on_a_key_the_agent_never_writes_is_not_observed(
    attached: _Attached,
) -> None:
    attached.start()
    spec = attached.spec(SESSION + '  correlate: {account_id: "{{session.conversation_id}}"}\n')

    result = attached.run(spec)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not observed" in result.stdout


@pytest.mark.timeout(40)
def test_attach_mode_without_instrumentation_is_not_observed(attached: _Attached) -> None:
    attached.start("--no-traces")

    result = attached.run(attached.spec())

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not observed: nothing was written to" in result.stdout


@pytest.mark.timeout(40)
def test_session_hook_that_cannot_load_exits_2_before_any_agent(attached: _Attached) -> None:
    marker = attached.root / "STARTED"
    (attached.root / "agent.highbar.yaml").write_text(
        f"""\
agent:
  start: "{sys.executable} -c \\"open('STARTED', 'w')\\""
  endpoint: "http://127.0.0.1:{attached.port}/chat"
  session:
    setup: {{python: "no_such_module_for_highbar:open"}}
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect: {{response: {{contains: "ok"}}}}
""",
        encoding="utf-8",
    )

    result = attached.run("agent.highbar.yaml")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot import 'no_such_module_for_highbar'" in result.stdout
    assert not marker.exists()
