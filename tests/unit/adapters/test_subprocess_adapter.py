"""Tests for the managed agent process, against a real stand-in server.

Mocks hid every bug the review found here (a pipe nobody drained, orphans left on
the port, an exit nobody noticed), so these tests start real processes.
"""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from highbar.adapters.ports import accepts_connections
from highbar.adapters.subprocess_adapter import AgentStartError, SubprocessAdapter

SERVER = Path(__file__).resolve().parents[2] / "fixtures" / "server.py"
PYTHON = sys.executable

MakeAdapter = Callable[..., tuple[SubprocessAdapter, int]]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


@pytest.fixture
def make_adapter(tmp_path: Path) -> Iterator[MakeAdapter]:
    """Build adapters for the stand-in server; every one is stopped after the test."""
    made: list[SubprocessAdapter] = []

    def make(
        *server_args: str,
        start_cmd: str | None = None,
        health: bool = True,
        host: str = "127.0.0.1",
        startup_timeout: int = 15000,
        env: dict[str, str] | None = None,
    ) -> tuple[SubprocessAdapter, int]:
        port = _free_port()
        command = start_cmd or " ".join([PYTHON, str(SERVER), "--port", str(port), *server_args])
        adapter = SubprocessAdapter(
            start_cmd=command.format(port=port),
            endpoint=f"http://{host}:{port}/chat",
            log_path=tmp_path / "logs" / "agent.log",
            health_url=f"http://127.0.0.1:{port}/health" if health else None,
            startup_timeout=startup_timeout,
            env=env,
        )
        made.append(adapter)
        return adapter, port

    yield make
    for adapter in made:
        adapter.stop()


class TestStart:
    def test_p2_busy_port_is_refused(self, make_adapter: MakeAdapter) -> None:
        """An agent already on the port would answer for this one, and a guardrail passes."""
        adapter, port = make_adapter()
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", port))
            squatter.listen()

            with pytest.raises(AgentStartError, match=f"port {port} is already in use"):
                adapter.start()

        assert not adapter.log_path.exists(), "no process may be started"

    def test_p4_early_exit_shows_log_tail_fast(self, make_adapter: MakeAdapter) -> None:
        adapter, _ = make_adapter("--exit-code", "3", startup_timeout=15000)
        started = time.monotonic()

        with pytest.raises(AgentStartError) as raised:
            adapter.start()

        assert time.monotonic() - started < 3
        assert "exited with code 3" in str(raised.value)
        assert "boom-marker" in str(raised.value)

    def test_start_that_exits_zero_suggests_attach_mode(self, make_adapter: MakeAdapter) -> None:
        """`docker compose up -d` returns 0 at once: waiting out the timeout helps nobody."""
        adapter, _ = make_adapter("--exit-code", "0")

        with pytest.raises(AgentStartError, match="attach"):
            adapter.start()

    def test_p1_chatty_agent_does_not_block(self, make_adapter: MakeAdapter) -> None:
        """Output goes to a file: an undrained pipe used to freeze the agent at 64 KB."""
        adapter, _ = make_adapter("--print-bytes", "1000000")

        adapter.start()

        assert adapter.log_path.stat().st_size >= 1_000_000

    def test_p5_without_health_waits_for_port(self, make_adapter: MakeAdapter) -> None:
        adapter, port = make_adapter("--delay", "1", health=False)

        adapter.start()

        assert accepts_connections("127.0.0.1", port)

    def test_localhost_endpoint_reaches_ipv4_only_listener(self, make_adapter: MakeAdapter) -> None:
        adapter, port = make_adapter(health=False, host="localhost")

        adapter.start()

        assert accepts_connections("localhost", port)

    def test_timeout_reports_the_log_tail(self, make_adapter: MakeAdapter) -> None:
        adapter, _ = make_adapter("--print-bytes", "10", "--delay", "30", startup_timeout=1000)

        with pytest.raises(AgentStartError, match="not ready after 1.0s") as raised:
            adapter.start()

        assert "xxxxxxxxxx" in str(raised.value)

    def test_env_is_injected(self, make_adapter: MakeAdapter) -> None:
        server = f"{PYTHON} {SERVER} --port {{port}}"
        adapter, _ = make_adapter(
            start_cmd=f"sh -c 'echo traces=$HIGHBAR_TRACES_URL; exec {server}'",
            env={"HIGHBAR_TRACES_URL": "/tmp/traces.jsonl"},
        )

        adapter.start()

        assert "traces=/tmp/traces.jsonl" in adapter.tail()

    def test_logs_dir_gets_gitignore(self, make_adapter: MakeAdapter) -> None:
        adapter, _ = make_adapter()

        adapter.start()

        assert (adapter.log_path.parent / ".gitignore").read_text().strip() == "*"


class TestStop:
    def test_p3_stop_kills_whole_process_group(self, make_adapter: MakeAdapter) -> None:
        """`sh -c`, `npm run` and `make` used to leave the real agent behind on the port."""
        adapter, port = make_adapter(
            start_cmd=f"sh -c '{PYTHON} {SERVER} --port {{port}}; echo done'"
        )
        adapter.start()

        adapter.stop()

        assert _wait_until(lambda: not accepts_connections("127.0.0.1", port), 6)

    def test_stop_is_idempotent(self, make_adapter: MakeAdapter) -> None:
        adapter, _ = make_adapter()
        adapter.start()

        adapter.stop()
        adapter.stop()

    def test_stop_before_start_is_a_no_op(self, make_adapter: MakeAdapter) -> None:
        adapter, _ = make_adapter()

        adapter.stop()


class TestTail:
    def test_tail_reads_only_the_end_of_a_large_log(self, tmp_path: Path) -> None:
        """Hundreds of MB of log must not be loaded to show the last 40 lines."""
        log = tmp_path / "agent.log"
        line_body = "y" * 990
        with log.open("w") as f:
            for number in range(5000):  # ~5 MB
                f.write(f"{number:06d} {line_body}\n")
        adapter = SubprocessAdapter(start_cmd="true", endpoint="http://127.0.0.1:1/", log_path=log)

        last_40 = adapter.tail(40).splitlines()
        as_many_as_fit = adapter.tail(1000).splitlines()

        assert [line[:6] for line in last_40] == [f"{n:06d}" for n in range(4960, 5000)]
        # Only the last 64 KB are read: with 1 KB lines, about 65 of them.
        assert len(as_many_as_fit) <= 66
        assert as_many_as_fit[-1].startswith("004999")

    def test_tail_of_a_missing_log_is_empty(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(
            start_cmd="true", endpoint="http://127.0.0.1:1/", log_path=tmp_path / "none.log"
        )

        assert adapter.tail() == ""
