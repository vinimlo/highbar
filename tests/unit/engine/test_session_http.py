"""HB-13: a session hook over HTTP, so the runner need not live in the agent's image."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from highbar.domain.models import HttpHook, SessionConfig
from highbar.engine.session import SessionContext, SessionError, SessionHooks

CTX = SessionContext(test_id="abc", test_name="cancel order", spec_path="specs/a.highbar.yaml")


class _Server:
    """Answers every request with one reply, and keeps what it was sent."""

    def __init__(self, status: int = 200, body: Any = None, *, raw: bytes | None = None) -> None:
        self.status = status
        self.content = raw if raw is not None else json.dumps(body).encode()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, content=self.content)


def _hooks(
    setup: Any = None,
    teardown: Any = None,
    handler: Any = None,
    *,
    timeout_ms: int = 10_000,
) -> SessionHooks:
    hooks = SessionHooks(
        SessionConfig(setup=setup, teardown=teardown),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=timeout_ms,
        transport=httpx.MockTransport(handler) if handler else None,
    )
    hooks.load()
    return hooks


class TestSetup:
    def test_hb13_setup_posts_and_its_json_object_is_the_session(self) -> None:
        server = _Server(body={"conversation_id": "c-1"})
        hook = HttpHook(url="http://agent.test/sessions")

        session = _hooks(setup=hook, handler=server).setup(CTX)

        assert session == {"conversation_id": "c-1"}
        assert server.requests[0].method == "POST"
        assert str(server.requests[0].url) == "http://agent.test/sessions"

    def test_hb13_placeholders_are_rendered_in_url_headers_and_body(self) -> None:
        server = _Server(body={"conversation_id": "c-1"})
        hook = HttpHook(
            url="http://agent.test/sessions?label={{test.name}}",
            headers={"X-Run": "{{test.id}}"},
            body={"test": "{{test.id}}", "tags": ["{{test.name}}"]},
        )

        _hooks(setup=hook, handler=server).setup(CTX)

        sent = server.requests[0]
        assert sent.url.params["label"] == "cancel order"
        assert sent.headers["X-Run"] == "abc"
        assert json.loads(sent.content) == {"test": "abc", "tags": ["cancel order"]}

    def test_hb13_no_body_sends_none(self) -> None:
        server = _Server(body={})
        _hooks(setup=HttpHook(url="http://agent.test/s", method="GET"), handler=server).setup(CTX)

        assert server.requests[0].method == "GET"
        assert server.requests[0].content == b""

    def test_hb13_a_status_that_is_not_2xx_is_an_error_with_the_body(self) -> None:
        server = _Server(status=500, raw=b"database is down")

        with pytest.raises(
            SessionError,
            match=r"session\.setup: POST http://agent.test/s returned HTTP 500: database is down",
        ):
            _hooks(setup=HttpHook(url="http://agent.test/s"), handler=server).setup(CTX)

    def test_hb13_a_redirect_is_not_followed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "http://elsewhere.test/s"})

        with pytest.raises(SessionError, match=r"HTTP 302.*redirected to http://elsewhere\.test/s"):
            _hooks(setup=HttpHook(url="http://agent.test/s"), handler=handler).setup(CTX)

    @pytest.mark.parametrize("raw", [b"", b"[1, 2]", b"not json", b"null"])
    def test_hb13_setup_needs_a_json_object(self, raw: bytes) -> None:
        server = _Server(raw=raw)

        with pytest.raises(SessionError, match="session.setup must return a JSON object"):
            _hooks(setup=HttpHook(url="http://agent.test/s"), handler=server).setup(CTX)

    def test_hb13_returned_headers_must_be_an_object(self) -> None:
        server = _Server(body={"headers": "Bearer x"})

        with pytest.raises(SessionError, match="'headers' that is not an object"):
            _hooks(setup=HttpHook(url="http://agent.test/s"), handler=server).setup(CTX)

    def test_hb13_an_unreachable_url_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(SessionError, match=r"session\.setup: cannot reach http://agent.test/s"):
            _hooks(setup=HttpHook(url="http://agent.test/s"), handler=handler).setup(CTX)

    def test_hb13_a_read_timeout_is_agent_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with pytest.raises(SessionError, match=r"did not finish in 0\.2s \(agent\.timeout\)"):
            _hooks(
                setup=HttpHook(url="http://agent.test/s"), handler=handler, timeout_ms=200
            ).setup(CTX)

    def test_hb13_a_reply_that_trickles_stops_at_agent_timeout(self) -> None:
        """httpx bounds each read, not the whole reply."""

        def trickle() -> Iterator[bytes]:
            for _ in range(50):
                time.sleep(0.02)
                yield b" "

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=trickle())

        started = time.monotonic()
        with pytest.raises(SessionError, match=r"did not finish in 0\.2s \(agent\.timeout\)"):
            _hooks(
                setup=HttpHook(url="http://agent.test/s"), handler=handler, timeout_ms=200
            ).setup(CTX)
        assert time.monotonic() - started < 0.8

    def test_hb13_a_rendered_header_with_a_newline_is_refused_unseen(self) -> None:
        """Usually a token read with its newline: the value is never shown."""
        server = _Server(body={})
        hook = HttpHook(url="http://agent.test/s", headers={"Authorization": "{{session.token}}"})
        hooks = _hooks(teardown=hook, handler=server)

        with pytest.raises(SessionError, match="control character") as raised:
            hooks.teardown(CTX, {"token": "Bearer s3cr3t\n"})
        assert "s3cr3t" not in str(raised.value)
        assert server.requests == []


class TestTeardown:
    def test_hb13_teardown_gets_the_session_values(self) -> None:
        server = _Server(raw=b"")
        hook = HttpHook(
            url="http://agent.test/sessions/{{session.conversation_id}}", method="DELETE"
        )

        _hooks(teardown=hook, handler=server).teardown(CTX, {"conversation_id": "c-9"})

        assert server.requests[0].method == "DELETE"
        assert str(server.requests[0].url) == "http://agent.test/sessions/c-9"

    def test_hb13_teardown_that_is_not_2xx_is_an_error(self) -> None:
        server = _Server(status=404, raw=b"no such conversation")

        with pytest.raises(SessionError, match=r"session\.teardown: DELETE .* returned HTTP 404"):
            _hooks(
                teardown=HttpHook(url="http://agent.test/s", method="DELETE"), handler=server
            ).teardown(CTX, {})


class TestValuesInTheUrlAreData:
    """Review of #28: a value from the session cannot move the request.

    A teardown's values come from the setup's reply, which is the agent under test.
    Pasted raw into the URL, they chose the host (and sent it the hook's headers) or
    the resource the DELETE reached.
    """

    @pytest.mark.parametrize(
        ("value", "path"),
        [
            ("../../admin/users", "/test/conversations/..%2F..%2Fadmin%2Fusers"),
            ("c-1#x", "/test/conversations/c-1%23x"),
            ("c-1?all=1", "/test/conversations/c-1%3Fall%3D1"),
            ("@evil.test/x", "/test/conversations/%40evil.test%2Fx"),
            ("café 1", "/test/conversations/caf%C3%A9%201"),
        ],
    )
    def test_hb13_a_session_value_is_one_encoded_piece_of_the_url(
        self, value: str, path: str
    ) -> None:
        server = _Server(raw=b"")
        hook = HttpHook(url="http://a.test/test/conversations/{{session.id}}", method="DELETE")

        _hooks(teardown=hook, handler=server).teardown(CTX, {"id": value})

        (sent,) = server.requests
        assert sent.url.host == "a.test"
        assert sent.url.raw_path.decode().split("?")[0] == path

    def test_hb13_a_value_in_the_query_stays_in_its_parameter(self) -> None:
        server = _Server(body={})
        hook = HttpHook(url="http://a.test/s?label={{test.name}}&mode=new", method="GET")
        ctx = SessionContext(test_id="abc", test_name="a&mode=old", spec_path="s.yaml")

        _hooks(setup=hook, handler=server).setup(ctx)

        assert server.requests[0].url.params.get_list("mode") == ["new"]
        assert server.requests[0].url.params["label"] == "a&mode=old"

    @pytest.mark.parametrize("value", ["..", "."])
    def test_hb13_a_value_that_is_a_path_step_is_refused(self, value: str) -> None:
        server = _Server(raw=b"")
        hook = HttpHook(url="http://a.test/test/conversations/{{session.id}}", method="DELETE")

        with pytest.raises(SessionError, match=r"session\.teardown: \{\{session\.id\}\}"):
            _hooks(teardown=hook, handler=server).teardown(CTX, {"id": value})
        assert server.requests == []


class TestHeadersOfTheHook:
    def test_hb13_a_lowercase_content_type_is_sent_once(self) -> None:
        server = _Server(body={})
        hook = HttpHook(
            url="http://a.test/s", headers={"content-type": "application/xml"}, body="<x/>"
        )

        _hooks(setup=hook, handler=server).setup(CTX)

        assert server.requests[0].headers.get_list("content-type") == ["application/xml"]

    def test_hb13_a_header_outside_ascii_is_a_session_error_without_the_value(self) -> None:
        server = _Server(body={})
        hook = HttpHook(url="http://a.test/s", headers={"X-Test": "{{test.name}}"})
        ctx = SessionContext(test_id="abc", test_name="cancelar ação", spec_path="s.yaml")

        with pytest.raises(SessionError, match=r"session\.setup: the header X-Test") as raised:
            _hooks(setup=hook, handler=server).setup(ctx)
        assert "ação" not in str(raised.value)
        assert server.requests == []
