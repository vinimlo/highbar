"""A local proxy between an agent and its model provider, reading each call's usage (HB-30).

The agent's model client points at it, through the base-URL variables the SDKs read
(``OPENAI_BASE_URL``, ``ANTHROPIC_BASE_URL``). Each request goes to the real upstream
unchanged, and the reply comes back unchanged, streaming included. On the way, the proxy
reads the usage the provider reports and writes one `llm` record per call to the trace
file, with ``source: "proxy"``.

A call whose usage it cannot read is recorded with ``read: false`` and the reason in
``unread``: it is never written as zero tokens. The record is in the trace file before
the client has the last byte of the reply, so an agent that writes its `turn` after the
reply has come back writes it after the call's record.

The proxy never logs a header, and never writes a key or a body to the trace or a log.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import socket
import threading
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx

from highbar._narrow import as_dict

Protocol = Literal["openai", "anthropic"]

TEST_HEADER = "X-Highbar-Test"

# A reply larger than this, decoded, is not read: holding it would only use memory.
MAX_READ_BYTES = 50_000_000

# Headers that belong to one connection, not to the message (RFC 9110, 7.6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# The encodings the proxy can read, so it asks the upstream for no other.
_READABLE_ENCODINGS = ("gzip", "deflate", "identity")


@dataclass(frozen=True)
class Upstream:
    """Where one prefix of the proxy goes: ``<proxy url>/<name>/...`` to ``base_url/...``.

    ``protocol`` says how its replies are read; None passes every call through unread.
    ``provider`` is written as the record's ``provider``, which the price table uses
    to tell apart a model name that several providers share.
    """

    base_url: str
    protocol: Protocol | None = None
    provider: str | None = None


@dataclass
class _Usage:
    """What one reply said it consumed. None is unknown, never zero."""

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None
    cost: float | None = None


def _count(value: Any) -> int | None:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too long for a float
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _label(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _openai_usage(body: Mapping[str, Any], usage: Mapping[str, Any]) -> _Usage:
    """`chat/completions` (prompt/completion) or `responses` (input/output), cache included."""
    raw_input = usage["prompt_tokens"] if "prompt_tokens" in usage else usage.get("input_tokens")
    raw_output = (
        usage["completion_tokens"] if "completion_tokens" in usage else usage.get("output_tokens")
    )
    details = as_dict(usage.get("prompt_tokens_details")) or as_dict(
        usage.get("input_tokens_details")
    )
    return _Usage(
        model=_label(body.get("model")),
        input_tokens=_count(raw_input),
        output_tokens=_count(raw_output),
        cache_tokens=_count(details.get("cached_tokens")) if details else None,
        cost=_cost(usage.get("cost")),  # OpenRouter's, in USD
    )


def _anthropic_input(usage: Mapping[str, Any]) -> int | None:
    """Anthropic's input leaves the cache out; the contract's input has it in.

    A cache field that is absent or null is a call without cache, which is 0. One that
    holds something else cannot be read, and neither can the input.
    """
    total = _count(usage.get("input_tokens"))
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        raw = usage.get(key)
        part = 0 if raw is None else _count(raw)
        if total is None or part is None:
            return None
        total += part
    return total


def _anthropic_usage(body: Mapping[str, Any], usage: Mapping[str, Any]) -> _Usage:
    raw_cache = usage.get("cache_read_input_tokens")
    return _Usage(
        model=_label(body.get("model")),
        input_tokens=_anthropic_input(usage),
        output_tokens=_count(usage.get("output_tokens")),
        cache_tokens=0 if raw_cache is None else _count(raw_cache),
    )


def _endpoint(protocol: Protocol | None, method: str, path: str) -> str | None:
    """Which reply format a call has, or None for a call the proxy does not read."""
    if method != "POST":
        return None
    path = path.rstrip("/")
    if protocol == "openai":
        if path.endswith("/chat/completions"):
            return "chat"
        if path.endswith("/responses"):
            return "responses"
    if protocol == "anthropic" and path.endswith("/messages"):
        return "messages"
    return None


STREAM_WITHOUT_USAGE = "the stream ended without usage"


class _Reading:
    """A reply's usage, read from its bytes as they go by to the client.

    A JSON reply is read whole at the end. A stream (``text/event-stream``) is read event
    by event: OpenAI's last chunk with ``usage``, the Responses API's ``response.*``
    events that carry the response, and Anthropic's ``message_start`` (input, cache)
    and ``message_delta`` (output). ``finish`` gives what was read, and why the count
    is not whole when it is not.
    """

    def __init__(self, endpoint: str, content_type: str, content_encoding: str) -> None:
        self.endpoint = endpoint
        self.stream = content_type.split(";")[0].strip().lower() == "text/event-stream"
        self.unread: str | None = None
        self._body = bytearray()  # a JSON reply, or the stream's unfinished line
        self._size = 0
        self._data: list[str] = []  # the data lines of the event being read
        self._model: str | None = None
        self._usage: _Usage | None = None
        self._start_usage: dict[str, Any] = {}  # Anthropic's message_start
        self._delta_usage: dict[str, Any] = {}  # Anthropic's message_delta, merged
        encoding = content_encoding.strip().lower()
        self._decoder: Any = None
        if encoding in ("gzip", "x-gzip"):
            self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._decoder = zlib.decompressobj(zlib.MAX_WBITS)
        elif encoding not in ("", "identity"):
            self.unread = f"the reply's '{encoding}' encoding is not one the proxy reads"

    def feed(self, raw: bytes) -> None:
        if self.unread is not None:
            return
        try:
            data = self._decoder.decompress(raw) if self._decoder is not None else raw
        except zlib.error:
            self.unread = "the reply's compressed body could not be decoded"
            return
        self._body.extend(data)
        if self.stream:
            self._lines()
            held = len(self._body)
        else:
            self._size += len(data)
            held = self._size
        if held > MAX_READ_BYTES:
            self.unread = f"the reply held more than {MAX_READ_BYTES} bytes to read"
            self._body.clear()

    def _lines(self, *, last: bool = False) -> None:
        """Read every whole line of the stream; at its end, the unfinished one too."""
        while True:
            newline = self._body.find(b"\n")
            if newline < 0:
                if not last:
                    return
                line, self._body = bytes(self._body), bytearray()
                self._line(line)
                self._event()
                return
            line = bytes(self._body[:newline])
            del self._body[: newline + 1]
            self._line(line)

    def _line(self, raw: bytes) -> None:
        line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
        if not line:
            self._event()
        elif line.startswith("data:"):
            value = line[5:]
            self._data.append(value[1:] if value.startswith(" ") else value)

    def _event(self) -> None:
        """One event ended: read its data, if it is an object."""
        data, self._data = "\n".join(self._data), []
        if not data or data == "[DONE]":
            return
        try:
            event = as_dict(json.loads(data))
        except ValueError:
            return
        if event is None:
            return
        if self.endpoint == "messages":
            self._anthropic_event(event)
            return
        response = as_dict(event.get("response"))  # the Responses API's response.* events
        if response is not None:
            event = response
        self._model = _label(event.get("model")) or self._model
        usage = as_dict(event.get("usage"))
        if usage is not None:
            self._usage = _openai_usage(event, usage)

    def _anthropic_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = as_dict(event.get("message")) or {}
            self._model = _label(message.get("model")) or self._model
            self._start_usage = as_dict(message.get("usage")) or {}
        elif kind == "message_delta":
            usage = as_dict(event.get("usage")) or {}
            # Its counts are the totals so far: a later one replaces an earlier one.
            self._delta_usage.update({k: v for k, v in usage.items() if v is not None})

    def finish(self) -> tuple[_Usage | None, str | None]:
        """What was read, and why the count is not whole (None when it is)."""
        if self.unread is not None:
            return None, self.unread
        if self.stream:
            return self._finish_stream()
        try:
            body = as_dict(json.loads(bytes(self._body)))
        except (ValueError, UnicodeDecodeError):
            body = None
        if body is None:
            return None, "the reply was not a JSON object"
        usage = as_dict(body.get("usage"))
        if usage is None:
            return _Usage(model=_label(body.get("model"))), "the reply had no usage"
        if self.endpoint == "messages":
            return _anthropic_usage(body, usage), None
        return _openai_usage(body, usage), None

    def _finish_stream(self) -> tuple[_Usage | None, str | None]:
        self._lines(last=True)
        if self.endpoint == "messages":
            if "output_tokens" not in self._delta_usage:
                # message_start's output_tokens is a placeholder, not the count.
                return _Usage(model=self._model), STREAM_WITHOUT_USAGE
            merged = {**self._start_usage, **self._delta_usage}
            usage = _anthropic_usage({"model": self._model}, merged)
            return usage, None
        if self._usage is None:
            return _Usage(model=self._model), STREAM_WITHOUT_USAGE
        if self._usage.model is None:
            self._usage.model = self._model
        return self._usage, None


def _request_model(body: bytes) -> str | None:
    try:
        request = as_dict(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        return None
    return _label(request.get("model")) if request is not None else None


def _with_stream_usage(body: bytes) -> bytes:
    """An OpenAI chat stream asks for its usage, unless it says something already.

    Without ``stream_options.include_usage`` the stream has no usage, and the call would
    be unread. One that sets it, to true or to false, is sent as it is.
    """
    try:
        request = as_dict(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        return body
    if request is None or request.get("stream") is not True:
        return body
    options = request.get("stream_options")
    if options is None:
        options = {}
    elif as_dict(options) is None or "include_usage" in options:
        return body
    request["stream_options"] = {**options, "include_usage": True}
    return json.dumps(request, ensure_ascii=False).encode()


def _readable_encodings(value: str | None) -> str:
    """The client's Accept-Encoding, less what the proxy could not read back."""
    if value is None:
        return "identity"
    kept = [
        part.strip()
        for part in value.split(",")
        if part.split(";")[0].strip().lower() in _READABLE_ENCODINGS
    ]
    return ", ".join(kept) or "identity"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, proxy: LlmProxy) -> None:
        self.proxy = proxy
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        with self.connections_lock:
            self.connections.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        with self.connections_lock:
            self.connections.discard(request)
        super().shutdown_request(request)

    def drop_connections(self) -> None:
        """Close every client connection, a kept-alive idle one included."""
        with self.connections_lock:
            connections = list(self.connections)
        for connection in connections:
            with contextlib.suppress(OSError):  # already closed by the client
                connection.shutdown(socket.SHUT_RDWR)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 900  # an idle kept-alive connection, or a client that stopped sending

    @property
    def proxy(self) -> LlmProxy:
        return cast(_Server, self.server).proxy

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    return bytes(body)
                body.extend(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _refuse(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message, "type": "highbar_llm_proxy"}}).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        proxy = self.proxy
        parts = urlsplit(self.path)
        name, _, rest = parts.path.lstrip("/").partition("/")
        upstream = proxy.upstreams.get(name)
        body = self._read_body()
        if upstream is None:
            self._refuse(404, f"highbar's LLM proxy has no upstream named '{name}'")
            return
        path = "/" + rest
        target = upstream.base_url.rstrip("/") + (path if rest else "")
        if parts.query:
            target += "?" + parts.query
        endpoint = _endpoint(upstream.protocol, self.command, path)

        connection_headers = {
            token.strip().lower()
            for token in (self.headers.get("Connection") or "").split(",")
            if token.strip()
        }
        headers = [
            (key, value)
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP
            and key.lower() not in connection_headers
            and key.lower() not in ("host", "content-length")
        ]
        if endpoint is not None:
            headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"]
            headers.append(
                ("Accept-Encoding", _readable_encodings(self.headers.get("Accept-Encoding")))
            )
        if endpoint == "chat":
            body = _with_stream_usage(body)
        test = self.headers.get(TEST_HEADER)
        call = _Call(proxy, upstream, endpoint, test, _request_model(body) if endpoint else None)

        request = proxy.client.build_request(self.command, target, headers=headers, content=body)
        try:
            response = proxy.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            reason = f"the '{name}' upstream could not be reached ({type(exc).__name__})"
            call.record(None, reason, error=reason)
            self._refuse(502, f"highbar's LLM proxy: {reason}")
            return
        try:
            self._relay(response, call)
        finally:
            response.close()

    def _relay(self, response: httpx.Response, call: _Call) -> None:
        reading = (
            _Reading(
                call.endpoint,
                response.headers.get("Content-Type") or "",
                response.headers.get("Content-Encoding") or "",
            )
            if call.endpoint is not None
            else None
        )
        error = f"HTTP {response.status_code}" if response.status_code >= 400 else None
        self.send_response_only(response.status_code, response.reason_phrase)
        upstream_connection = {
            token.strip().lower()
            for token in (response.headers.get("Connection") or "").split(",")
            if token.strip()
        }
        for key, value in response.headers.multi_items():
            lower = key.lower()
            if lower in _HOP_BY_HOP or lower in upstream_connection or lower == "content-length":
                continue
            self.send_header(key, value)
        no_body = (
            self.command == "HEAD"
            or response.status_code in (204, 304)
            or response.status_code < 200
        )
        raw_length = response.headers.get("Content-Length")
        length = int(raw_length) if raw_length is not None and raw_length.isdigit() else None
        if "transfer-encoding" in response.headers:
            length = None
        if no_body:
            if raw_length is not None:
                self.send_header("Content-Length", raw_length)
            self.end_headers()
            call.finish(reading, error=error)
            return
        chunked = length is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        self.end_headers()

        sent = 0
        try:
            for raw in response.iter_raw():
                if not raw:
                    continue
                if reading is not None:
                    reading.feed(raw)
                if length is not None and sent + len(raw) >= length:
                    call.finish(reading, error=error)  # before the client has the last byte
                self._write(raw, chunked)
                sent += len(raw)
        except httpx.HTTPError as exc:
            call.record(None, f"the reply was cut off ({type(exc).__name__})", error=error)
            self.close_connection = True
            return
        except OSError:
            call.record(None, "the client closed the connection before the reply ended", error)
            self.close_connection = True
            return
        call.finish(reading, error=error)
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write(self, data: bytes, chunked: bool) -> None:
        if chunked:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


class _Call:
    """One call through the proxy, recorded once."""

    def __init__(
        self,
        proxy: LlmProxy,
        upstream: Upstream,
        endpoint: str | None,
        test: str | None,
        request_model: str | None,
    ) -> None:
        self.proxy = proxy
        self.upstream = upstream
        self.endpoint = endpoint
        self.test = test
        self.request_model = request_model
        self.started = time.monotonic()
        self.recorded = False

    def finish(self, reading: _Reading | None, *, error: str | None) -> None:
        if self.recorded:
            return
        if reading is None:
            return
        usage, unread = reading.finish()
        self.record(usage, unread, error=error)

    def record(self, usage: _Usage | None, unread: str | None, error: str | None) -> None:
        if self.recorded:
            return
        self.recorded = True
        record: dict[str, Any] = {"v": 1, "type": "llm", "ts": time.time(), "source": "proxy"}
        model = (usage.model if usage is not None else None) or self.request_model
        if model is not None:
            record["model"] = model
        if self.upstream.provider is not None:
            record["provider"] = self.upstream.provider
        if usage is not None:
            for key in ("input_tokens", "output_tokens", "cache_tokens"):
                value = getattr(usage, key)
                if value is not None:
                    record[key] = value
            if usage.cost is not None:
                record["cost"] = usage.cost
                record["currency"] = "USD"
            missing = [
                key for key in ("input_tokens", "output_tokens") if getattr(usage, key) is None
            ]
            if missing and unread is None:
                unread = f"the reply's usage had no readable {' or '.join(missing)}"
        record["read"] = unread is None
        if unread is not None:
            record["unread"] = unread
        record["latency_ms"] = round((time.monotonic() - self.started) * 1000, 3)
        if error is not None:
            record["error"] = error
        if self.test:
            record["context"] = {"test": self.test}
        self.proxy.write(record)


class LlmProxy:
    """The proxy: ``start()`` listens on a free 127.0.0.1 port, ``stop()`` frees it.

    ``upstreams`` maps a path prefix to where its calls go: with ``{"openai": ...}``,
    the agent's base URL is ``base_url("openai")``. ``write_errors`` counts records the
    trace file did not take: each one is a call no budget saw.
    """

    def __init__(
        self,
        trace_path: str | Path,
        upstreams: Mapping[str, Upstream],
        *,
        timeout: float = 600.0,
    ) -> None:
        self.trace_path = Path(trace_path)
        self.upstreams = dict(upstreams)
        self.timeout = timeout
        self.write_errors = 0
        self._write_lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("the LLM proxy is not running")
        return self._client

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the LLM proxy is not running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def base_url(self, name: str) -> str:
        """The base URL an agent's client uses for the upstream ``name``."""
        if name not in self.upstreams:
            raise KeyError(f"no upstream named '{name}'")
        return f"{self.url}/{name}"

    def start(self) -> None:
        if self._server is not None:
            return
        client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=30.0),
            follow_redirects=False,
            trust_env=False,
        )
        # httpx adds these when the request has none; the proxy sends what the agent sent.
        for header in ("Accept", "Accept-Encoding", "User-Agent"):
            del client.headers[header]
        self._client = client
        self._server = _Server(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.1,), name="highbar-llm-proxy", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.drop_connections()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def __enter__(self) -> LlmProxy:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def write(self, record: Mapping[str, Any]) -> None:
        """Append one record: one write per line, so two threads never interleave."""
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        with self._write_lock:
            try:
                fd = os.open(self.trace_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                self.write_errors += 1
