"""HB-30: the local LLM proxy reads each call's usage at the source and records it.

The upstream here is a standard-library HTTP server that answers what each test sets,
and keeps what it received, so a test can check that the request went through unchanged.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from highbar.adapters.file_collector import FileTraceCollector
from highbar.adapters.llm_proxy import LlmProxy, Upstream
from highbar.domain.models import LlmCall

pytestmark = pytest.mark.timeout(30)

KEY = "sk-test-0123456789abcdef-secret"
SECRET_PROMPT = "the launch code is 4471"


@dataclass
class Answer:
    """What the fake upstream answers: a whole body, or SSE events sent one by one."""

    body: bytes = b""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict[str, str])
    events: list[bytes] | None = None  # sent chunked, one write each


@dataclass
class Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeUpstream:
    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {}
        self.received: list[Received] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream.received.append(
                    Received(self.command, self.path, dict(self.headers.items()), body)
                )
                answer = upstream.answers.get(self.path.split("?")[0], Answer(status=404))
                self.send_response(answer.status)
                for name, value in answer.headers.items():
                    self.send_header(name, value)
                if answer.events is None:
                    self.send_header("Content-Length", str(len(answer.body)))
                    self.end_headers()
                    self.wfile.write(answer.body)
                    return
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for event in answer.events:
                    self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    fake = FakeUpstream()
    yield fake
    fake.close()


@pytest.fixture
def trace(tmp_path: Path) -> Path:
    return tmp_path / "traces.jsonl"


@pytest.fixture
def proxy(upstream: FakeUpstream, trace: Path) -> Iterator[LlmProxy]:
    running = LlmProxy(
        trace,
        {
            "openai": Upstream(f"{upstream.url}/v1", protocol="openai", provider="openai"),
            "anthropic": Upstream(upstream.url, protocol="anthropic", provider="anthropic"),
        },
    )
    running.start()
    yield running
    running.stop()


def _json(value: Any) -> bytes:
    return json.dumps(value).encode()


def _calls(trace: Path) -> list[LlmCall]:
    return FileTraceCollector(trace).collect(since=0).llms


def _post(url: str, body: dict[str, Any], **headers: str) -> httpx.Response:
    return httpx.post(
        url,
        content=_json(body),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", **headers},
        timeout=10,
    )


CHAT_REQUEST = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": SECRET_PROMPT}],
}

CHAT_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
        "prompt_tokens_details": {"cached_tokens": 800},
    },
}


def test_hb30_an_openai_chat_call_is_recorded_with_its_tokens_and_model(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    upstream.answers["/v1/chat/completions"] = Answer(_json(CHAT_REPLY))

    reply = _post(f"{proxy.base_url('openai')}/chat/completions", CHAT_REQUEST)

    assert reply.status_code == 200
    (call,) = _calls(trace)
    assert call.source == "proxy"
    assert call.read is True
    assert call.model == "gpt-4o-mini-2024-07-18"  # the response's, not the request's
    assert call.provider == "openai"
    assert (call.input_tokens, call.output_tokens, call.cache_tokens) == (1000, 200, 800)
    assert call.cost is None  # OpenAI says no cost: the price table works it out


def test_hb30_openrouters_cost_is_recorded(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    reply = {**CHAT_REPLY, "model": "openai/gpt-4o-mini"}
    reply["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0021}
    upstream.answers["/v1/chat/completions"] = Answer(_json(reply))

    _post(f"{proxy.base_url('openai')}/chat/completions", CHAT_REQUEST)

    (call,) = _calls(trace)
    assert (call.input_tokens, call.output_tokens, call.cache_tokens) == (10, 5, None)
    assert (call.cost, call.currency) == (0.0021, "USD")


def test_hb30_an_openai_responses_call_is_recorded(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    upstream.answers["/v1/responses"] = Answer(
        _json(
            {
                "id": "resp_1",
                "object": "response",
                "model": "gpt-4.1-2025-04-14",
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 70,
                    "input_tokens_details": {"cached_tokens": 100},
                },
            }
        )
    )

    _post(f"{proxy.base_url('openai')}/responses", {"model": "gpt-4.1", "input": SECRET_PROMPT})

    (call,) = _calls(trace)
    assert call.read is True and call.model == "gpt-4.1-2025-04-14"
    assert (call.input_tokens, call.output_tokens, call.cache_tokens) == (500, 70, 100)


ANTHROPIC_REQUEST = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": SECRET_PROMPT}],
}


def test_hb30_an_anthropic_call_counts_the_cache_in_the_input(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # Anthropic leaves the cache out of input_tokens; the contract's input includes it.
    upstream.answers["/v1/messages"] = Answer(
        _json(
            {
                "id": "msg_1",
                "type": "message",
                "model": "claude-sonnet-4-5-20250929",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 50,
                    "output_tokens": 20,
                },
            }
        )
    )

    _post(f"{proxy.base_url('anthropic')}/v1/messages", ANTHROPIC_REQUEST, **{"x-api-key": KEY})

    (call,) = _calls(trace)
    assert call.read is True and call.provider == "anthropic"
    assert call.model == "claude-sonnet-4-5-20250929"
    assert (call.input_tokens, call.output_tokens, call.cache_tokens) == (1050, 20, 900)


def test_hb30_the_request_and_the_reply_go_through_unchanged(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # Odd spacing and key order: re-serialized JSON would not give these bytes back.
    body = b'{"id":"x",  "model":"gpt-4o-mini","usage":{"prompt_tokens":3,"completion_tokens":4}}'
    upstream.answers["/v1/chat/completions"] = Answer(
        body, status=201, headers={"Content-Type": "application/json", "X-Request-Id": "req-9"}
    )
    sent = b'{"model": "gpt-4o-mini",   "messages": []}'

    reply = httpx.post(
        f"{proxy.base_url('openai')}/chat/completions?api-version=7",
        content=sent,
        headers={"Authorization": f"Bearer {KEY}", "X-Custom": "kept"},
        timeout=10,
    )

    assert reply.status_code == 201
    assert reply.content == body
    assert reply.headers["x-request-id"] == "req-9"
    (received,) = upstream.received
    assert received.path == "/v1/chat/completions?api-version=7"
    assert received.body == sent
    assert received.headers["Authorization"] == f"Bearer {KEY}"
    assert received.headers["X-Custom"] == "kept"


def test_hb30_the_test_header_becomes_the_records_test_context(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    upstream.answers["/v1/chat/completions"] = Answer(_json(CHAT_REPLY))

    _post(f"{proxy.base_url('openai')}/chat/completions", CHAT_REQUEST)
    _post(
        f"{proxy.base_url('openai')}/chat/completions", CHAT_REQUEST, **{"X-Highbar-Test": "t-42"}
    )

    untagged, tagged = _calls(trace)
    assert untagged.context == {}
    assert tagged.context == {"test": "t-42"}


def test_hb30_stop_frees_the_port(upstream: FakeUpstream, trace: Path) -> None:
    proxy = LlmProxy(trace, {"openai": Upstream(f"{upstream.url}/v1", protocol="openai")})
    proxy.start()
    upstream.answers["/v1/chat/completions"] = Answer(_json(CHAT_REPLY))
    port = int(proxy.url.rsplit(":", 1)[1])
    endpoint = f"{proxy.base_url('openai')}/chat/completions"
    # A kept-alive connection must not hold the port either.
    with httpx.Client(timeout=10) as client:
        client.post(endpoint, content=_json(CHAT_REQUEST))

        proxy.stop()

        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        with pytest.raises(httpx.TransportError):
            client.post(endpoint, content=b"{}")


def test_hb30_the_proxy_listens_on_loopback_only(upstream: FakeUpstream, trace: Path) -> None:
    proxy = LlmProxy(trace, {"openai": Upstream(upstream.url, protocol="openai")})
    proxy.start()
    try:
        assert proxy.url.startswith("http://127.0.0.1:")
        assert proxy.base_url("openai") == f"{proxy.url}/openai"
    finally:
        proxy.stop()


def _sse(*events: dict[str, Any] | str, named: bool = False) -> bytes:
    """Server-sent events: `data: <json>` each, with `event: <type>` when ``named``."""
    out = b""
    for event in events:
        if isinstance(event, str):
            out += f"data: {event}\n\n".encode()
            continue
        if named:
            out += f"event: {event['type']}\n".encode()
        out += b"data: " + _json(event) + b"\n\n"
    return out


def _pieces(stream: bytes, size: int = 7) -> list[bytes]:
    """The stream in small pieces, so that lines and events are split across reads."""
    return [stream[i : i + size] for i in range(0, len(stream), size)]


SSE_HEADERS = {"Content-Type": "text/event-stream"}


def _chat_chunk(content: str | None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    choices = [] if content is None else [{"index": 0, "delta": {"content": content}}]
    chunk: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "gpt-4o-mini-2024-07-18",
        "choices": choices,
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


CHAT_STREAM = _sse(
    _chat_chunk("Hel"),
    _chat_chunk("lo"),
    _chat_chunk(None, {"prompt_tokens": 1000, "completion_tokens": 200}),
    "[DONE]",
)


def test_hb30_an_openai_stream_asks_for_usage_and_reads_the_last_chunk(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    upstream.answers["/v1/chat/completions"] = Answer(
        headers=SSE_HEADERS, events=_pieces(CHAT_STREAM)
    )

    reply = _post(f"{proxy.base_url('openai')}/chat/completions", {**CHAT_REQUEST, "stream": True})

    assert reply.content == CHAT_STREAM
    (received,) = upstream.received
    assert json.loads(received.body)["stream_options"] == {"include_usage": True}
    assert json.loads(received.body)["messages"] == CHAT_REQUEST["messages"]
    (call,) = _calls(trace)
    assert call.read is True and call.model == "gpt-4o-mini-2024-07-18"
    assert (call.input_tokens, call.output_tokens) == (1000, 200)


def test_hb30_a_stream_that_asks_for_usage_goes_through_byte_for_byte(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    upstream.answers["/v1/chat/completions"] = Answer(headers=SSE_HEADERS, events=[CHAT_STREAM])
    sent = b'{"model":"gpt-4o-mini","stream":true,  "stream_options":{"include_usage":true}}'

    httpx.post(f"{proxy.base_url('openai')}/chat/completions", content=sent, timeout=10)

    (received,) = upstream.received
    assert received.body == sent
    (call,) = _calls(trace)
    assert call.read is True


def test_hb30_an_openai_stream_without_usage_is_unread_not_zero(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # A server that ignores stream_options: the stream ends and no chunk said the usage.
    stream = _sse(_chat_chunk("Hel"), _chat_chunk("lo"), "[DONE]")
    upstream.answers["/v1/chat/completions"] = Answer(headers=SSE_HEADERS, events=[stream])

    reply = _post(f"{proxy.base_url('openai')}/chat/completions", {**CHAT_REQUEST, "stream": True})

    assert reply.content == stream
    (call,) = _calls(trace)
    assert call.read is False
    assert call.unread is not None and "stream ended without usage" in call.unread
    assert call.model == "gpt-4o-mini-2024-07-18"
    assert (call.input_tokens, call.output_tokens, call.cost) == (None, None, None)


def test_hb30_an_openai_responses_stream_is_read_from_response_completed(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    stream = _sse(
        {"type": "response.created", "response": {"model": "gpt-4.1-2025-04-14", "usage": None}},
        {"type": "response.output_text.delta", "delta": "hi"},
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-4.1-2025-04-14",
                "usage": {"input_tokens": 500, "output_tokens": 70},
            },
        },
        named=True,
    )
    upstream.answers["/v1/responses"] = Answer(headers=SSE_HEADERS, events=_pieces(stream))

    reply = _post(
        f"{proxy.base_url('openai')}/responses",
        {"model": "gpt-4.1", "input": SECRET_PROMPT, "stream": True},
    )

    assert reply.content == stream
    assert "stream_options" not in json.loads(upstream.received[0].body)
    (call,) = _calls(trace)
    assert call.read is True and call.model == "gpt-4.1-2025-04-14"
    assert (call.input_tokens, call.output_tokens) == (500, 70)


ANTHROPIC_START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "model": "claude-sonnet-4-5-20250929",
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 1},
    },
}


def test_hb30_an_anthropic_stream_is_read_from_message_start_and_message_delta(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    stream = _sse(
        ANTHROPIC_START,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 20},
        },
        {"type": "message_stop"},
        named=True,
    )
    upstream.answers["/v1/messages"] = Answer(headers=SSE_HEADERS, events=_pieces(stream))

    reply = _post(
        f"{proxy.base_url('anthropic')}/v1/messages", {**ANTHROPIC_REQUEST, "stream": True}
    )

    assert reply.content == stream
    (call,) = _calls(trace)
    assert call.read is True and call.model == "claude-sonnet-4-5-20250929"
    assert (call.input_tokens, call.output_tokens, call.cache_tokens) == (1000, 20, 900)


def test_hb30_an_anthropic_stream_cut_before_message_delta_is_unread(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # message_start's output_tokens is a placeholder: taken as the count, it would be 1.
    stream = _sse(ANTHROPIC_START, {"type": "message_stop"}, named=True)
    upstream.answers["/v1/messages"] = Answer(headers=SSE_HEADERS, events=[stream])

    _post(f"{proxy.base_url('anthropic')}/v1/messages", {**ANTHROPIC_REQUEST, "stream": True})

    (call,) = _calls(trace)
    assert call.read is False
    assert call.unread is not None and "stream ended without usage" in call.unread
    assert call.output_tokens is None
