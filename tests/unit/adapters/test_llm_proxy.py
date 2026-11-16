"""HB-30: the local LLM proxy reads each call's usage at the source and records it.

The upstream here is a standard-library HTTP server that answers what each test sets,
and keeps what it received, so a test can check that the request went through unchanged.
"""

from __future__ import annotations

import gzip
import http.client
import json
import logging
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
    cut: bool = False  # the connection closes before the stream's end
    # What a stream is when the request sets stream_options.include_usage, as OpenAI
    # answers: one more chunk, with `choices: []` and the usage. None: always `events`.
    usage_events: list[bytes] | None = None


@dataclass
class Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


def _asks_for_usage(body: bytes) -> bool:
    try:
        options = json.loads(body).get("stream_options") or {}
    except ValueError:
        return False
    return options.get("include_usage") is True


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
                events = answer.events
                if answer.usage_events is not None and _asks_for_usage(body):
                    events = answer.usage_events
                for event in events:
                    self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                    self.wfile.flush()
                if answer.cut:
                    self.close_connection = True
                    return
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


CHAT_STREAM_WITHOUT_USAGE = _sse(_chat_chunk("Hel"), _chat_chunk("lo"), "[DONE]")


def test_hb30_a_stream_the_proxy_asked_usage_for_reaches_the_client_as_it_would_directly(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # The usage chunk has `choices: []`: a loop on `chunk.choices[0]` raises IndexError on
    # it, so an agent the proxy hands it would fail only because highbar is in front.
    upstream.answers["/v1/chat/completions"] = Answer(
        headers=SSE_HEADERS,
        events=_pieces(CHAT_STREAM_WITHOUT_USAGE),
        usage_events=_pieces(CHAT_STREAM),
    )
    request = {**CHAT_REQUEST, "stream": True}

    direct = _post(f"{upstream.url}/v1/chat/completions", request)
    proxied = _post(f"{proxy.base_url('openai')}/chat/completions", request)

    assert direct.content == CHAT_STREAM_WITHOUT_USAGE
    assert proxied.content == direct.content
    sent_direct, sent_proxied = upstream.received
    assert "stream_options" not in json.loads(sent_direct.body)
    assert json.loads(sent_proxied.body)["stream_options"] == {"include_usage": True}
    assert json.loads(sent_proxied.body)["messages"] == CHAT_REQUEST["messages"]
    assert sent_proxied.headers["Accept-Encoding"] == "identity"  # the chunk comes out plain
    (call,) = _calls(trace)
    assert call.read is True and call.model == "gpt-4o-mini-2024-07-18"
    assert (call.input_tokens, call.output_tokens) == (1000, 200)


def test_hb30_usage_on_a_chunk_with_choices_is_read_and_forwarded(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # Some providers put the usage on the last chunk that still has choices: the client
    # gets that chunk either way, so it goes through as it is.
    last = _chat_chunk("lo", {"prompt_tokens": 30, "completion_tokens": 4})
    stream = _sse(_chat_chunk("Hel"), last, "[DONE]")
    upstream.answers["/v1/chat/completions"] = Answer(headers=SSE_HEADERS, events=[stream])

    reply = _post(f"{proxy.base_url('openai')}/chat/completions", {**CHAT_REQUEST, "stream": True})

    assert reply.content == stream
    (call,) = _calls(trace)
    assert call.read is True and (call.input_tokens, call.output_tokens) == (30, 4)


def test_hb30_a_stream_that_asks_for_usage_goes_through_byte_for_byte(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # The client asked for the usage chunk: it gets it, as it would directly.
    upstream.answers["/v1/chat/completions"] = Answer(
        headers=SSE_HEADERS, events=_pieces(CHAT_STREAM)
    )
    sent = b'{"model":"gpt-4o-mini","stream":true,  "stream_options":{"include_usage":true}}'

    reply = httpx.post(f"{proxy.base_url('openai')}/chat/completions", content=sent, timeout=10)

    assert reply.content == CHAT_STREAM
    (received,) = upstream.received
    assert received.body == sent
    (call,) = _calls(trace)
    assert call.read is True and (call.input_tokens, call.output_tokens) == (1000, 200)


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


def test_hb30_a_path_the_proxy_does_not_read_is_passed_through_and_recorded_unread(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    body = _json({"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 8}})
    upstream.answers["/v1/embeddings"] = Answer(body)
    sent = _json({"model": "text-embedding-3-small", "input": SECRET_PROMPT})

    reply = httpx.post(f"{proxy.base_url('openai')}/embeddings", content=sent, timeout=10)

    assert reply.content == body
    assert upstream.received[0].body == sent
    (call,) = _calls(trace)
    assert call.source == "proxy" and call.read is False
    assert call.unread is not None and "/embeddings" in call.unread
    # Its usage is there, and still not taken: a format the proxy does not know.
    assert (call.input_tokens, call.output_tokens) == (None, None)


def test_hb30_an_upstream_of_unknown_protocol_is_passed_through_unread(
    upstream: FakeUpstream, trace: Path
) -> None:
    upstream.answers["/v1beta/models/gemini:generateContent"] = Answer(_json(CHAT_REPLY))
    with LlmProxy(trace, {"gemini": Upstream(f"{upstream.url}/v1beta")}) as proxy:
        reply = _post(f"{proxy.base_url('gemini')}/models/gemini:generateContent", CHAT_REQUEST)

    assert reply.json() == CHAT_REPLY
    (call,) = _calls(trace)
    assert call.read is False and call.input_tokens is None


def test_hb30_an_upstream_error_is_recorded_unread_with_its_status(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    error = _json({"error": {"message": "rate limited"}})
    upstream.answers["/v1/chat/completions"] = Answer(error, status=429)

    reply = _post(f"{proxy.base_url('openai')}/chat/completions", CHAT_REQUEST)

    assert reply.status_code == 429 and reply.content == error
    (call,) = _calls(trace)
    assert call.read is False and call.error == "HTTP 429"


def test_hb30_an_unreachable_upstream_is_a_502_and_an_unread_call(trace: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]  # nothing listens here once the socket closes
    with LlmProxy(trace, {"openai": Upstream(f"http://127.0.0.1:{closed}/v1", "openai")}) as proxy:
        reply = _post(f"{proxy.base_url('openai')}/chat/completions", CHAT_REQUEST)

    assert reply.status_code == 502
    assert "could not be reached" in reply.json()["error"]["message"]
    (call,) = _calls(trace)
    assert call.read is False and call.error is not None


def test_hb30_a_gzipped_reply_reaches_the_client_as_sent_and_is_read(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    compressed = gzip.compress(_json(CHAT_REPLY))
    upstream.answers["/v1/chat/completions"] = Answer(
        compressed, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}
    )

    with httpx.Client(timeout=10) as client:
        request = client.build_request(
            "POST",
            f"{proxy.base_url('openai')}/chat/completions",
            content=_json(CHAT_REQUEST),
            headers={"Accept-Encoding": "br, gzip"},
        )
        reply = client.send(request, stream=True)
        raw = b"".join(reply.iter_raw())
        reply.close()

    assert raw == compressed
    # Brotli would be a reply the proxy cannot read: it asks only for what it can.
    assert upstream.received[0].headers["Accept-Encoding"] == "gzip"
    (call,) = _calls(trace)
    assert call.read is True and (call.input_tokens, call.output_tokens) == (1000, 200)


def test_hb30_no_key_or_body_reaches_the_trace_or_the_logs(
    upstream: FakeUpstream,
    proxy: LlmProxy,
    trace: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_reply = "the vault opens at dawn"
    reply = {**CHAT_REPLY, "choices": [{"message": {"content": secret_reply}}]}
    upstream.answers["/v1/chat/completions"] = Answer(_json(reply))
    upstream.answers["/v1/messages"] = Answer(_json({"usage": {}, "content": secret_reply}))

    port = int(proxy.url.rsplit(":", 1)[1])

    def send(path: str, body: dict[str, Any], **headers: str) -> None:
        # http.client logs nothing: httpx, here, would log the test's own URLs.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("POST", path, body=_json(body), headers=headers)
            connection.getresponse().read()
        finally:
            connection.close()

    # The key in a header, in another header and in the query (as Google's API takes it).
    auth = {"Authorization": f"Bearer {KEY}"}
    send(f"/openai/chat/completions?key={KEY}", CHAT_REQUEST, **auth)
    send("/anthropic/v1/messages", ANTHROPIC_REQUEST, **{"x-api-key": KEY})
    send(f"/openai/embeddings?key={KEY}", CHAT_REQUEST, **auth)
    send(f"/nowhere?key={KEY}", CHAT_REQUEST, **auth)
    # A request line the proxy cannot parse is answered 400, and not echoed to a log.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        raw.sendall(f"GARBAGE {KEY} {SECRET_PROMPT} HTTP/1.1\r\n\r\n".encode())
        assert raw.recv(100).startswith(b"HTTP/1.1 400")

    written = trace.read_text(encoding="utf-8")
    out, err = capfd.readouterr()
    for place, text in {
        "trace": written,
        "logs": caplog.text,
        "stdout": out,
        "stderr": err,
    }.items():
        for secret in (KEY, SECRET_PROMPT, secret_reply, "Authorization", "x-api-key"):
            assert secret not in text, f"{secret!r} in the {place}"
    assert len(_calls(trace)) == 3


def test_hb30_a_stream_the_upstream_cuts_is_unread_even_after_its_usage(
    upstream: FakeUpstream, proxy: LlmProxy, trace: Path
) -> None:
    # The usage chunk came, and the stream never ended: the call did not end as read.
    upstream.answers["/v1/chat/completions"] = Answer(
        headers=SSE_HEADERS, events=[CHAT_STREAM], cut=True
    )

    with pytest.raises(httpx.RemoteProtocolError):
        _post(f"{proxy.base_url('openai')}/chat/completions", {**CHAT_REQUEST, "stream": True})

    (call,) = _calls(trace)
    assert call.read is False
    assert call.unread is not None and "cut off" in call.unread
