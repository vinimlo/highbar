"""The request highbar sends and how it reads the reply (spec D5; findings P6, P7)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from highbar.adapters.http_client import AgentResponseError, HTTPClient
from highbar.adapters.ports import accepts_connections
from highbar.domain.models import RequestConfig
from highbar.engine.placeholders import Values

ENDPOINT = "http://agent.test/chat"
SERVER = Path(__file__).resolve().parents[2] / "fixtures" / "server.py"


def _values(session: dict[str, Any] | None = None) -> Values:
    return Values(message="hi", test_id="t-1", test_name="greeting", session=session or {})


class _Agent:
    """A MockTransport that records requests and answers with a fixed response."""

    def __init__(self, response: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.requests: list[httpx.Request] = []
        self._response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self._response):
            return self._response(request)
        return self._response

    def client(self, **kwargs: Any) -> HTTPClient:
        return HTTPClient(ENDPOINT, transport=httpx.MockTransport(self.handler), **kwargs)

    @property
    def body(self) -> Any:
        return json.loads(self.requests[-1].content)


def _json(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


class TestRequest:
    def test_default_request_posts_the_message_as_json(self) -> None:
        agent = _Agent(_json({"response": "hello"}))

        assert agent.client().send("hi", values=_values()) == "hello"

        request = agent.requests[0]
        assert request.method == "POST"
        assert str(request.url) == ENDPOINT
        assert agent.body == {"message": "hi"}

    def test_custom_method_headers_and_body_keep_types(self) -> None:
        agent = _Agent(_json({"response": "ok"}))
        request = RequestConfig(
            method="PUT",
            headers={"Authorization": "Bearer abc", "X-Conversation": "{{session.cid}}"},
            body={"text": "{{message}}", "conversation_id": "{{session.cid}}", "lang": "pt"},
        )

        agent.client(request=request).send("hi", values=_values({"cid": 42}))

        sent = agent.requests[0]
        assert sent.method == "PUT"
        assert sent.headers["authorization"] == "Bearer abc"
        assert sent.headers["x-conversation"] == "42"
        assert agent.body == {"text": "hi", "conversation_id": 42, "lang": "pt"}

    def test_session_headers_are_added_and_win(self) -> None:
        agent = _Agent(_json({"response": "ok"}))
        request = RequestConfig(headers={"Authorization": "Bearer from-spec", "X-A": "a"})
        session = {"headers": {"authorization": "Bearer from-session", "X-B": "b"}}

        agent.client(request=request).send("hi", values=_values(session))

        headers = agent.requests[0].headers
        assert headers["authorization"] == "Bearer from-session"
        assert (headers["x-a"], headers["x-b"]) == ("a", "b")

    def test_x_highbar_test_header_is_always_sent(self) -> None:
        agent = _Agent(_json({"response": "ok"}))
        request = RequestConfig(headers={"X-Highbar-Test": "spoofed"})

        agent.client(request=request).send("hi", values=_values())

        assert agent.requests[0].headers["x-highbar-test"] == "t-1"

    def test_text_body_is_sent_as_text(self) -> None:
        agent = _Agent(_json({"response": "ok"}))

        agent.client(request=RequestConfig(body="{{message}}")).send("hi", values=_values())

        assert agent.requests[0].content == b"hi"


class TestResponse:
    def test_p6_non_json_response_is_used_as_text(self) -> None:
        agent = _Agent(httpx.Response(200, text="plain hello"))

        assert agent.client().send("hi", values=_values()) == "plain hello"

    def test_p6_json_without_known_keys_is_an_error_listing_them(self) -> None:
        agent = _Agent(_json({"data": {"reply": "x"}, "status": "ok"}))

        with pytest.raises(AgentResponseError, match=r"keys: data, status.*agent\.response"):
            agent.client().send("hi", values=_values())

    def test_known_keys_in_order(self) -> None:
        agent = _Agent(_json({"text": "second", "message": "first"}))

        assert agent.client().send("hi", values=_values()) == "first"

    def test_json_string_is_the_text(self) -> None:
        assert _Agent(_json("just text")).client().send("hi", values=_values()) == "just text"

    def test_response_path_with_an_index(self) -> None:
        agent = _Agent(_json({"choices": [{"message": {"content": "from the model"}}]}))

        reply = agent.client(response_path="choices.0.message.content").send("hi", values=_values())

        assert reply == "from the model"

    def test_response_path_that_does_not_resolve_names_the_keys(self) -> None:
        agent = _Agent(_json({"data": {"text": "x"}}))

        with pytest.raises(AgentResponseError, match=r"agent\.response 'data\.reply'.*keys: text"):
            agent.client(response_path="data.reply").send("hi", values=_values())

    def test_response_path_on_a_non_json_reply_is_an_error(self) -> None:
        agent = _Agent(httpx.Response(200, text="<html>oops</html>"))

        with pytest.raises(AgentResponseError, match="is not a JSON object"):
            agent.client(response_path="data.reply").send("hi", values=_values())

    def test_non_text_value_at_an_explicit_path_is_rendered_as_json(self) -> None:
        agent = _Agent(_json({"response": {"items": [1, 2]}}))

        reply = agent.client(response_path="response").send("hi", values=_values())

        assert reply == '{"items": [1, 2]}'


class TestFailures:
    def test_http_status_error_shows_status_and_body_not_headers(self) -> None:
        """CI logs are public enough: the token must not end up in them."""
        agent = _Agent(httpx.Response(401, text="invalid token"))
        request = RequestConfig(headers={"Authorization": "Bearer s3cr3t"})

        with pytest.raises(AgentResponseError) as raised:
            agent.client(request=request).send("hi", values=_values())

        message = str(raised.value)
        assert "HTTP 401" in message
        assert ENDPOINT in message
        assert "invalid token" in message
        assert "s3cr3t" not in message

    def test_unreachable_agent_is_named(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(AgentResponseError, match=f"cannot reach {ENDPOINT}"):
            _Agent(refuse).client().send("hi", values=_values())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def slow_agent() -> Iterator[str]:
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--slow-chat", "3", "--no-traces"]
    )
    try:
        deadline = time.monotonic() + 10
        while not accepts_connections("127.0.0.1", port) and time.monotonic() < deadline:
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}/chat"
    finally:
        server.kill()
        server.wait()


@pytest.mark.timeout(20)
def test_p7_timeout_is_configurable(slow_agent: str) -> None:
    client = HTTPClient(slow_agent, timeout_ms=500)
    started = time.monotonic()

    with pytest.raises(AgentResponseError, match=r"no response in 0\.5s \(agent\.timeout\)"):
        client.send("hi", values=_values())

    assert time.monotonic() - started < 2


class _Trickle(httpx.SyncByteStream):
    """A reply that keeps sending keep-alive lines: each read is quick, the whole is not."""

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(20):
            time.sleep(0.2)
            yield b": keep-alive\n"


class TestReviewFindings:
    """PR 5 review: replies that are not a reply, timeouts that do not bound, secrets."""

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_c2_redirect_is_an_error_that_says_where(self, status: int) -> None:
        agent = _Agent(httpx.Response(status, headers={"Location": "http://agent.test/chat/"}))

        with pytest.raises(AgentResponseError, match=rf"HTTP {status} .*redirected to "
                           r"http://agent.test/chat/; set agent.endpoint to that URL"):  # fmt: skip
            agent.client().send("hi", values=_values())

    @pytest.mark.parametrize("status", [200, 202, 204])
    def test_c2_empty_body_is_an_error(self, status: int) -> None:
        agent = _Agent(httpx.Response(status))

        with pytest.raises(AgentResponseError, match="empty body"):
            agent.client().send("hi", values=_values())

    def test_c2_non_success_status_below_400_is_an_error(self) -> None:
        agent = _Agent(httpx.Response(101, text="switching"))

        with pytest.raises(AgentResponseError, match="HTTP 101"):
            agent.client().send("hi", values=_values())

    @pytest.mark.parametrize("path", [None, "data.reply"])
    def test_i7_null_reply_is_an_error_not_the_word_null(self, path: str | None) -> None:
        data = {"response": None, "error": "quota"} if path is None else {"data": {"reply": None}}
        agent = _Agent(_json(data))

        with pytest.raises(AgentResponseError, match="null"):
            agent.client(response_path=path).send("hi", values=_values())

    def test_object_at_an_implicit_reply_key_is_an_error_with_the_hint(self) -> None:
        agent = _Agent(_json({"message": {"role": "assistant", "content": "hi"}}))

        with pytest.raises(AgentResponseError, match="set agent.response"):
            agent.client().send("hi", values=_values())

    @pytest.mark.parametrize("text", ["42", "true", "null", "[1, 2]", '"ok"'])
    def test_i6_plain_text_reply_is_the_text_even_when_it_parses_as_json(self, text: str) -> None:
        agent = _Agent(httpx.Response(200, text=text, headers={"Content-Type": "text/plain"}))

        assert agent.client().send("hi", values=_values()) == text

    def test_json_object_sent_as_text_is_still_read(self) -> None:
        """A hand-rolled agent returning json.dumps(...) as text/html."""
        agent = _Agent(
            httpx.Response(200, text='{"response": "hello"}', headers={"Content-Type": "text/html"})
        )

        assert agent.client().send("hi", values=_values()) == "hello"

    def test_invalid_json_under_a_json_content_type_is_an_error(self) -> None:
        agent = _Agent(
            httpx.Response(
                200,
                text='{"response": "Sorry, I can',
                headers={"Content-Type": "application/json"},
            )
        )

        with pytest.raises(AgentResponseError, match="not valid JSON"):
            agent.client().send("hi", values=_values())

    def test_i1_timeout_bounds_a_reply_that_keeps_trickling(self) -> None:
        agent = _Agent(httpx.Response(200, stream=_Trickle()))
        started = time.monotonic()

        with pytest.raises(AgentResponseError, match=r"no response in 0.5s \(agent.timeout\)"):
            agent.client(timeout_ms=500).send("hi", values=_values())

        assert time.monotonic() - started < 1.5

    def test_i1_reply_larger_than_the_cap_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("highbar.adapters.http_client.MAX_REPLY_BYTES", 1000)
        agent = _Agent(httpx.Response(200, text="x" * 5000))

        with pytest.raises(AgentResponseError, match="larger than"):
            agent.client().send("hi", values=_values())

    def test_i2_header_value_the_http_library_rejects_is_never_echoed(self) -> None:
        """A session header with a trailing newline: h11 refuses it, quoting the value."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        client = HTTPClient(f"http://127.0.0.1:{port}/chat", timeout_ms=2000)
        session = {"headers": {"Authorization": "Bearer s3cr3t-token\n"}}
        try:
            with pytest.raises(AgentResponseError) as info:
                client.send("hi", values=_values(session))
        finally:
            listener.close()

        assert "s3cr3t" not in str(info.value)
        assert "header" in str(info.value)
