"""highbar.asgi.Middleware: a `turn` per HTTP request, tagged with X-Highbar-Test."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from highbar import Highbar, asgi

ENV = "HIGHBAR_TRACES_URL"
Message = dict[str, Any]
App = Callable[
    [Message, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]],
    Awaitable[None],
]


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _of(path: Path, kind: str) -> list[dict[str, Any]]:
    return [r for r in _records(path) if r["type"] == kind]


@pytest.fixture
def traces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "traces.jsonl"
    monkeypatch.setenv(ENV, str(path))
    return path


def _http_scope(test_id: str | None = None) -> Message:
    headers = [(b"content-type", b"application/json")]
    if test_id is not None:
        headers.append((b"X-Highbar-Test", test_id.encode()))
    return {"type": "http", "method": "POST", "path": "/chat", "headers": headers}


async def _receive() -> Message:
    return {"type": "http.request", "body": b"{}", "more_body": False}


def _call(app: Any, scope: Message, on_send: Callable[[Message], None] | None = None) -> None:
    async def send(message: Message) -> None:
        if on_send is not None:
            on_send(message)

    asyncio.run(app(scope, _receive, send))


def _answering_app(hb: Highbar, *, before_answer: Callable[[], None] | None = None) -> Any:
    @hb.tool
    def lookup() -> str:
        return "found"

    async def app(scope: Message, receive: Any, send: Any) -> None:
        lookup()
        if before_answer is not None:
            before_answer()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": True})
        await send({"type": "http.response.body", "body": b"!", "more_body": False})

    return app


class TestMiddleware:
    def test_turn_is_written_before_the_response_reaches_the_client(self, traces: Path) -> None:
        """highbar collects as soon as the reply arrives: the turn must already be there."""
        hb = Highbar()
        seen_at_last_body: list[int] = []

        def on_send(message: Message) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                seen_at_last_body.append(len(_of(traces, "turn")))

        _call(asgi.Middleware(_answering_app(hb), hb=hb), _http_scope("t1"), on_send)

        assert seen_at_last_body == [1]
        assert len(_of(traces, "turn")) == 1

    def test_tools_called_by_the_handler_carry_the_test_id(self, traces: Path) -> None:
        hb = Highbar()

        _call(asgi.Middleware(_answering_app(hb), hb=hb), _http_scope("t1"))

        (tool,) = _of(traces, "tool")
        (turn,) = _of(traces, "turn")
        assert tool["context"] == {"test": "t1"}
        assert turn["context"] == {"test": "t1"}

    def test_request_without_the_header_writes_a_turn_without_test(self, traces: Path) -> None:
        hb = Highbar()

        _call(asgi.Middleware(_answering_app(hb), hb=hb), _http_scope())

        (turn,) = _of(traces, "turn")
        assert "context" not in turn

    def test_handler_exception_still_writes_the_turn(self, traces: Path) -> None:
        hb = Highbar()

        def explode() -> None:
            raise RuntimeError("handler broke")

        with pytest.raises(RuntimeError):
            _call(
                asgi.Middleware(_answering_app(hb, before_answer=explode), hb=hb), _http_scope("t1")
            )

        assert len(_of(traces, "turn")) == 1

    def test_non_http_scopes_pass_through(self, traces: Path) -> None:
        hb = Highbar()
        received: list[str] = []

        async def app(scope: Message, receive: Any, send: Any) -> None:
            received.append(scope["type"])

        middleware = asgi.Middleware(app, hb=hb)
        _call(middleware, {"type": "lifespan"})
        _call(middleware, {"type": "websocket", "path": "/ws", "headers": []})

        assert received == ["lifespan", "websocket"]
        assert _of(traces, "turn") == []

    def test_disabled_sdk_passes_everything_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV, raising=False)
        hb = Highbar()
        bodies: list[bytes] = []

        def on_send(message: Message) -> None:
            if message["type"] == "http.response.body":
                bodies.append(message["body"])

        _call(asgi.Middleware(_answering_app(hb), hb=hb), _http_scope("t1"), on_send)

        assert bodies == [b"ok", b"!"]
        assert list(tmp_path.iterdir()) == []


class TestFastAPI:
    def test_add_middleware_with_fastapi(self, traces: Path) -> None:
        """`app.add_middleware(Middleware, hb=hb)` works, for sync and async handlers."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        hb = Highbar()
        app = FastAPI()

        @hb.tool
        def find(order: str) -> str:
            return order

        @app.post("/async")
        async def async_chat() -> dict[str, str]:
            return {"response": find("a")}

        @app.post("/sync")
        def sync_chat() -> dict[str, str]:  # FastAPI runs it in a worker thread
            return {"response": find("s")}

        app.add_middleware(asgi.Middleware, hb=hb)

        with TestClient(app) as client:
            assert client.post("/async", headers={"X-Highbar-Test": "t-a"}).status_code == 200
            assert client.post("/sync", headers={"X-Highbar-Test": "t-s"}).status_code == 200

        tools = {t["args"]["order"]: t["context"]["test"] for t in _of(traces, "tool")}
        turns = [t["context"]["test"] for t in _of(traces, "turn")]
        assert tools == {"a": "t-a", "s": "t-s"}
        assert turns == ["t-a", "t-s"]


def test_s2_sdk_imports_only_the_standard_library() -> None:
    """`highbar[sdk]` used to promise zero dependencies and install four (S2)."""
    code = (
        "import sys, highbar; from highbar import Highbar, asgi; import highbar.sdk; "
        "print(sorted({'yaml', 'httpx', 'rich', 'typer'} & set(sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "[]"


def test_run_is_part_of_the_public_api() -> None:
    import highbar

    assert callable(highbar.run)
    assert set(highbar.__all__) == {"Highbar", "asgi", "run"}
