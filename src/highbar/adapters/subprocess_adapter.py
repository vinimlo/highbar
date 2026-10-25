"""Subprocess adapter — manages the agent process lifecycle (POSIX).

What it guarantees, each one a bug it used to have:

- The port is free before the agent starts. An old agent left on it would answer in
  this one's place, and a guardrail (`function_calls: []`) would pass with nothing
  instrumented.
- The agent's output goes to a log file, never a pipe: a pipe nobody reads fills at
  64 KB and freezes the agent mid-request.
- The agent runs in its own process group, and stopping kills the group: `sh -c`,
  `npm run` or `make` would otherwise leave the real server behind on the port.
- An agent that exits while starting is reported at once, with the end of its log,
  instead of after the whole startup timeout with nothing to read.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import httpx

from highbar.adapters.ports import accepts_connections, describe_listener, endpoint_address

_POLL_INTERVAL = 0.2
_STOP_GRACE = 5.0
_TAIL_BYTES = 64 * 1024


class AgentStartError(RuntimeError):
    """The agent did not come up. The message ends with the tail of its log, if any."""


class SubprocessAdapter:
    """Starts the agent as its own process group and stops the whole group."""

    def __init__(
        self,
        start_cmd: str,
        endpoint: str,
        log_path: Path,
        health_url: str | None = None,
        startup_timeout: int = 15000,
        env: dict[str, str] | None = None,
    ) -> None:
        self._start_cmd = start_cmd
        self._endpoint = endpoint
        self._log_path = Path(log_path)
        self._health_url = health_url
        self._startup_timeout_s = startup_timeout / 1000.0
        self._env = env
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def start(self) -> None:
        """Start the agent and wait until it is ready. Raises ``AgentStartError``."""
        host, port = endpoint_address(self._endpoint)
        if accepts_connections(host, port):
            who = describe_listener(port)
            busy = f"port {port} is already in use" + (f" by {who}" if who else "")
            raise AgentStartError(
                f"{busy}. Stop it first: tests would talk to it instead of the agent "
                "highbar starts."
            )

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = self._log_path.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")

        env = {**os.environ, **(self._env or {})}
        with open(self._log_path, "ab") as log:
            self._process = subprocess.Popen(
                shlex.split(self._start_cmd),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )

        try:
            self._wait_until_ready(host, port)
        except AgentStartError:
            self.stop()
            raise

    def stop(self) -> None:
        """Stop the agent's whole process group. Safe to call more than once."""
        process, self._process = self._process, None
        if process is None:
            return

        # start_new_session made the agent the leader of a group with its own id.
        group = process.pid
        _signal_group(group, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_GRACE
        while time.monotonic() < deadline:
            process.poll()  # reap the leader, or it counts as alive
            if not _group_alive(group):
                return
            time.sleep(0.1)
        _signal_group(group, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)

    def tail(self, lines: int = 40) -> str:
        """The last ``lines`` lines of the agent's log, reading at most its last 64 KB."""
        try:
            with open(self._log_path, "rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(0, size - _TAIL_BYTES))
                data = log.read()
        except FileNotFoundError:
            return ""
        return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])

    def _wait_until_ready(self, host: str, port: int) -> None:
        assert self._process is not None
        deadline = time.monotonic() + self._startup_timeout_s
        while True:
            code = self._process.poll()
            if code is not None:
                raise AgentStartError(self._exited_message(code))
            if self._ready(host, port):
                return
            if time.monotonic() >= deadline:
                waited_for = (
                    f"{self._health_url} to answer 200"
                    if self._health_url
                    else f"{host}:{port} to accept connections"
                )
                raise AgentStartError(
                    f"agent not ready after {self._startup_timeout_s}s "
                    f"(waited for {waited_for}){self._log_section()}"
                )
            time.sleep(_POLL_INTERVAL)

    def _ready(self, host: str, port: int) -> bool:
        if self._health_url is None:
            return accepts_connections(host, port)
        try:
            return httpx.get(self._health_url, timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def _exited_message(self, code: int) -> str:
        message = f"agent exited with code {code} before it was ready"
        if code == 0:
            message += (
                ". It exited cleanly, so `start` probably launches the agent in the "
                "background (docker compose up -d, a script that forks): remove `start` "
                "and point the spec at the running agent (attach mode)"
            )
        return message + self._log_section()

    def _log_section(self) -> str:
        tail = self.tail()
        if not tail:
            return f"\n(no output in {self._log_path})"
        return f"\n--- last lines of {self._log_path} ---\n{tail}"


def _signal_group(group: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, sig)


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
