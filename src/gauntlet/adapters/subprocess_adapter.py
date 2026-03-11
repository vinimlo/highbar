"""Subprocess adapter — manages the agent process lifecycle."""

from __future__ import annotations

import os
import shlex
import subprocess
import time

import httpx


class SubprocessAdapter:
    """Starts and stops the agent as a subprocess."""

    def __init__(
        self,
        start_cmd: str,
        health_url: str | None = None,
        startup_timeout: int = 15000,
        env: dict[str, str] | None = None,
    ) -> None:
        self._start_cmd = start_cmd
        self._health_url = health_url
        self._startup_timeout_s = startup_timeout / 1000.0
        self._env = env
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        """Start the agent subprocess and wait for health check."""
        env = {**os.environ, **(self._env or {})}

        self._process = subprocess.Popen(
            shlex.split(self._start_cmd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if self._health_url:
            self._wait_for_health()

    def stop(self) -> None:
        """Stop the agent subprocess."""
        if self._process is None:
            return

        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def _wait_for_health(self) -> None:
        """Poll health endpoint until it returns 200."""
        assert self._health_url is not None
        deadline = time.time() + self._startup_timeout_s
        interval = 0.2

        while time.time() < deadline:
            try:
                resp = httpx.get(self._health_url, timeout=2.0)
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass

            time.sleep(interval)

        self.stop()
        msg = f"Agent health check failed after {self._startup_timeout_s}s at {self._health_url}"
        raise TimeoutError(msg)
