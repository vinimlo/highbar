"""HB-39 v1: agent.timeout bounds a message to the agent from the moment it is sent.

httpx's timeout holds for each read, not for the whole reply, and the client's deadline
was checked only while the body was read. An agent that sends its status line and
headers a byte at a time kept a message of 0.5 s waiting for as long as it pleased: the
same shape v0 fixed for HTTP hooks (#63). These use a real socket: a mock transport has
no status line to send slowly.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from highbar.adapters.http_client import AgentResponseError, HTTPClient
from highbar.engine.placeholders import Values

REPLY = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
    b"X-Padding: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\nContent-Length: 13\r\n\r\n"
    b"you have days"
)


@pytest.fixture
def trickling(request: pytest.FixtureRequest) -> Iterator[str]:
    """An agent that sends its reply, status line and headers included, a byte at a
    time: every 0.1 s, or every ``request.param`` seconds when a test parametrizes it."""
    pause: float = getattr(request, "param", 0.1)
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.recv(65536)
                for byte in REPLY:
                    if stop.is_set():
                        break
                    try:
                        conn.sendall(bytes([byte]))
                    except OSError:
                        break
                    time.sleep(pause)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/chat"
    stop.set()
    thread.join(timeout=5)
    server.close()


def _values() -> Values:
    return Values(message="hi", test_id="abc", test_name="greeting", session={})


def test_hb39_a_message_with_slow_headers_stops_at_agent_timeout(trickling: str) -> None:
    client = HTTPClient(trickling, timeout_ms=500)

    started = time.monotonic()
    with pytest.raises(AgentResponseError, match=r"no response in 0\.5s \(agent\.timeout\)") as e:
        client.send("hi", values=_values())

    assert time.monotonic() - started < 1.5
    # The same cause as any other timeout, so --max-errors counts it (HB-34).
    assert e.value.cause is not None
    assert e.value.cause.kind == "timeout"


@pytest.mark.parametrize("trickling", [0.002], indirect=True)
def test_hb39_a_reply_that_comes_in_time_is_still_read(trickling: str) -> None:
    """The same agent, with time enough: about 0.3 s of bytes, within 5 s."""
    client = HTTPClient(trickling, timeout_ms=5_000)

    assert client.send("hi", values=_values()) == "you have days"
