"""HTTP client adapter — sends triggers to the agent endpoint."""

from __future__ import annotations

import httpx


class HTTPClient:
    """Sends trigger messages to the agent's HTTP endpoint."""

    def __init__(self, endpoint: str, timeout: float = 30.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    def send(self, trigger: str) -> str:
        """Send a trigger message and return the response text."""
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                self._endpoint,
                json={"message": trigger},
            )
            resp.raise_for_status()
            data = resp.json()

        # Try common response formats
        if isinstance(data, dict):
            for key in ("response", "message", "content", "text", "reply"):
                if key in data:
                    return str(data[key])

        return str(data)
