"""HTTP client adapter — sends a trigger the way the spec says, reads the reply text.

The request (method, headers, body) and where the reply text lives come from the
spec's ``agent`` block (D5). Every failure is an ``AgentResponseError`` whose
message says what came back, never the request's headers: they carry tokens, and
this output ends up in CI logs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from highbar.assertions.response import truncate
from highbar.domain.models import RequestConfig
from highbar.engine.paths import PathError, get_path
from highbar.engine.placeholders import Values, render

# Where the reply text usually is, when the spec does not say (agent.response).
REPLY_KEYS = ("response", "message", "content", "text", "reply")

TEST_HEADER = "X-Highbar-Test"


class AgentResponseError(RuntimeError):
    """The agent could not be reached, failed, or replied with no text to check."""


class HTTPClient:
    """Sends trigger messages to the agent's HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        request: RequestConfig | None = None,
        response_path: str | None = None,
        timeout_ms: int = 60000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._request = request or RequestConfig()
        self._response_path = response_path
        self._timeout_ms = timeout_ms
        self._transport = transport

    def send(self, message: str, *, values: Values) -> str:
        """Send ``message`` and return the reply text. Raises ``AgentResponseError``.

        Placeholders are resolved with ``values`` (raises ``PlaceholderError``).
        """
        headers = self._headers(values)
        body = render(self._request.body, values)
        content: dict[str, Any] = {}
        if isinstance(body, str):
            content["content"] = body.encode("utf-8")
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        else:
            content["json"] = body

        seconds = self._timeout_ms / 1000
        try:
            with httpx.Client(timeout=seconds, transport=self._transport) as client:
                reply = client.request(
                    self._request.method, self._endpoint, headers=headers, **content
                )
        except httpx.TimeoutException:
            raise AgentResponseError(f"no response in {seconds:g}s (agent.timeout)") from None
        except httpx.ConnectError as e:
            raise AgentResponseError(f"cannot reach {self._endpoint}: {e}") from None
        except httpx.HTTPError as e:
            raise AgentResponseError(f"request to {self._endpoint} failed: {e}") from None

        if reply.status_code >= 400:
            raise AgentResponseError(
                f"HTTP {reply.status_code} from {self._endpoint}: {truncate(reply.text)}"
            )
        return self._reply_text(reply.text)

    def _headers(self, values: Values) -> dict[str, str]:
        """The spec's headers, then the session's (which win), then the test id."""
        headers: dict[str, str] = {}

        def put(name: str, value: Any) -> None:
            for existing in [key for key in headers if key.lower() == name.lower()]:
                del headers[existing]
            headers[name] = value if isinstance(value, str) else json.dumps(value)

        for name, value in render(self._request.headers, values).items():
            put(name, value)
        session_headers = values.session.get("headers")
        if isinstance(session_headers, dict):
            for name, value in session_headers.items():
                put(str(name), value)
        put(TEST_HEADER, values.test_id)
        return headers

    def _reply_text(self, raw: str) -> str:
        try:
            data = json.loads(raw)
        except ValueError:
            if self._response_path is not None:
                raise AgentResponseError(
                    f"the reply is not JSON, so agent.response '{self._response_path}' "
                    f"cannot be read. Got: {truncate(raw)!r}"
                ) from None
            # A plain-text reply is the text to check (P6).
            return raw

        if self._response_path is not None:
            try:
                return _as_text(get_path(data, self._response_path))
            except PathError as e:
                raise AgentResponseError(f"agent.response '{self._response_path}': {e}") from None

        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in REPLY_KEYS:
                if key in data:
                    return _as_text(data[key])
            keys = ", ".join(str(key) for key in data) or "none"
            raise AgentResponseError(
                f"the JSON reply has none of {', '.join(REPLY_KEYS)} (keys: {keys}); "
                "set agent.response to the path of the reply text"
            )
        raise AgentResponseError(
            f"the JSON reply is a {type(data).__name__}, not an object with the reply text; "
            "set agent.response to its path"
        )


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
