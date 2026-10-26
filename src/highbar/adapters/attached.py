"""Attach mode: the agent is already running, and highbar neither starts nor stops it."""

from __future__ import annotations

from pathlib import Path

import httpx

from highbar.adapters.ports import accepts_connections, endpoint_address
from highbar.adapters.subprocess_adapter import AgentStartError


class AttachedAgent:
    """Checks the agent is reachable before the run, and leaves it running after."""

    log_path: Path | None = None

    def __init__(self, endpoint: str, health_url: str | None = None) -> None:
        self._endpoint = endpoint
        self._health_url = health_url

    def start(self) -> None:
        """Raise ``AgentStartError`` unless the agent answers.

        With ``health``, it must answer 200; without it, the endpoint's port must
        accept connections. Without this check, an agent nobody started would turn
        every test into an error instead of one line saying so.
        """
        if self._health_url is not None:
            try:
                status = httpx.get(self._health_url, timeout=5.0).status_code
            except httpx.HTTPError as e:
                raise AgentStartError(
                    f"health check {self._health_url} failed: {e}. In attach mode (no 'start' "
                    "in the spec) the agent must already be running"
                ) from None
            if status != 200:
                raise AgentStartError(f"health check {self._health_url} answered {status}")
            return

        host, port = endpoint_address(self._endpoint)
        if not accepts_connections(host, port):
            raise AgentStartError(
                f"nothing answers at {host}:{port}; in attach mode (no 'start' in the spec) "
                "the agent must already be running"
            )

    def stop(self) -> None:
        """Nothing to do: highbar did not start this agent."""
