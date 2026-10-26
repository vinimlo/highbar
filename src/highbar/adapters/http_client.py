"""HTTP client adapter — sends a trigger the way the spec says, reads the reply text.

The request (method, headers, body) and where the reply text lives come from the
spec's ``agent`` block (D5). Every failure is an ``AgentResponseError`` whose
message says what came back, never the request's headers: they carry tokens, and
this output ends up in CI logs.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from highbar._narrow import as_dict
from highbar.assertions.response import truncate
from highbar.domain.models import RequestConfig
from highbar.engine.paths import PathError, get_path
from highbar.engine.placeholders import Values, render

# A reply larger than this is not a chat reply; reading on would only use memory.
MAX_REPLY_BYTES = 10_000_000

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
        # httpx's timeout bounds each read, not the whole reply: a reply that keeps
        # trickling bytes would never end. The deadline bounds it.
        deadline = time.monotonic() + seconds
        try:
            with (
                httpx.Client(timeout=seconds, transport=self._transport) as client,
                client.stream(
                    self._request.method, self._endpoint, headers=headers, **content
                ) as reply,
            ):
                raw = _read(reply, deadline, seconds)
        except httpx.TimeoutException:
            raise AgentResponseError(f"no response in {seconds:g}s (agent.timeout)") from None
        except httpx.ConnectError as e:
            raise AgentResponseError(f"cannot reach {self._endpoint}: {e}") from None
        except httpx.LocalProtocolError:
            # The library quotes the offending header value whole, and it is usually a
            # token read with its trailing newline: say which kind of problem, never it.
            raise AgentResponseError(
                f"the request to {self._endpoint} could not be sent: a header value has a "
                "control character or leading or trailing whitespace (agent.request.headers "
                "or the session's headers); the value is not shown"
            ) from None
        except httpx.HTTPError as e:
            raise AgentResponseError(f"request to {self._endpoint} failed: {e}") from None

        text = raw.decode(reply.charset_encoding or "utf-8", errors="replace")
        status = reply.status_code
        if 300 <= status < 400:
            where = reply.headers.get("Location", "an unnamed location")
            raise AgentResponseError(
                f"HTTP {status} from {self._endpoint}: redirected to {where}; "
                "set agent.endpoint to that URL"
            )
        if not reply.is_success:
            raise AgentResponseError(f"HTTP {status} from {self._endpoint}: {truncate(text)}")
        if not raw.strip():
            # Checked as "", it would pass every not_contains and every guardrail on text.
            raise AgentResponseError(f"the agent replied with an empty body (HTTP {status})")
        content_type = reply.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return self._reply_text(text, declared_json=_is_json(content_type))

    def _headers(self, values: Values) -> dict[str, str]:
        """The spec's headers, then the session's (which win), then the test id."""
        headers: dict[str, str] = {}

        def put(name: str, value: Any) -> None:
            for existing in [key for key in headers if key.lower() == name.lower()]:
                del headers[existing]
            headers[name] = value if isinstance(value, str) else json.dumps(value)

        for name, value in render(self._request.headers, values).items():
            put(name, value)
        session_headers = as_dict(values.session.get("headers"))
        if session_headers is not None:
            for name, value in session_headers.items():
                put(str(name), value)
        put(TEST_HEADER, values.test_id)
        return headers

    def _reply_text(self, raw: str, *, declared_json: bool) -> str:
        """The text to check: plain text as it came (P6), or the reply text in the JSON.

        A JSON body is read as JSON when it says so in its Content-Type, or when it is
        an object (a hand-rolled agent may send ``json.dumps(...)`` as text). Plain text
        that happens to parse as a number, a list or ``null`` stays text.
        """
        data: Any
        try:
            data = json.loads(raw)
        except ValueError:
            if declared_json:
                raise AgentResponseError(
                    f"the reply says it is JSON but is not valid JSON. Got: {truncate(raw)!r}"
                ) from None
            data = None
            parsed = False
        else:
            parsed = declared_json or as_dict(data) is not None

        if not parsed:
            if self._response_path is not None:
                raise AgentResponseError(
                    f"the reply is not a JSON object, so agent.response "
                    f"'{self._response_path}' cannot be read. Got: {truncate(raw)!r}"
                )
            return raw

        if self._response_path is not None:
            try:
                value = get_path(data, self._response_path)
            except PathError as e:
                raise AgentResponseError(f"agent.response '{self._response_path}': {e}") from None
            if value is None:
                raise AgentResponseError(f"agent.response '{self._response_path}' is null")
            return _as_text(value)

        if (reply := as_dict(data)) is not None:
            for key in REPLY_KEYS:
                if key in reply:
                    return _implicit_text(key, reply[key])
            keys = ", ".join(str(key) for key in reply) or "none"
            raise AgentResponseError(
                f"the JSON reply has none of {', '.join(REPLY_KEYS)} (keys: {keys}); "
                "set agent.response to the path of the reply text"
            )
        if data is None:
            raise AgentResponseError("the reply is JSON null: there is no text to check")
        if isinstance(data, str):
            return data
        # A JSON number or list with no path to the text: it is the text.
        return raw


def _read(reply: httpx.Response, deadline: float, seconds: float) -> bytes:
    body = bytearray()
    for chunk in reply.iter_bytes():
        body += chunk
        if len(body) > MAX_REPLY_BYTES:
            raise AgentResponseError(
                f"the reply is larger than {MAX_REPLY_BYTES // 1_000_000} MB; "
                "highbar stops reading there"
            )
        if time.monotonic() > deadline:
            raise AgentResponseError(f"no response in {seconds:g}s (agent.timeout)")
    return bytes(body)


def _is_json(content_type: str) -> bool:
    return content_type == "application/json" or content_type.endswith("+json")


def _implicit_text(key: str, value: Any) -> str:
    """The value of a reply key highbar guessed: text, or a scalar, nothing else."""
    if value is None:
        raise AgentResponseError(f"the reply's '{key}' is null: there is no text to check")
    if isinstance(value, (dict, list)):
        # Compared as JSON, `contains` could match a field the agent echoed back.
        raise AgentResponseError(
            f"the reply's '{key}' is a {'list' if isinstance(value, list) else 'JSON object'}, "
            "not text; set agent.response to the path of the reply text"
        )
    return _as_text(value)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
