"""HB-39: agent.timeout bounds an HTTP hook from the moment it is sent.

httpx's timeout holds for each read, not for the whole reply, and the hook's deadline
was checked only while the body was read. A server that sends its status line and
headers a byte at a time kept a hook of 0.5 s waiting for 11.8 s (review of #28). These
use a real socket: a mock transport has no status line to send slowly.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from highbar.domain.models import HttpHook, RunConfig, SessionConfig
from highbar.engine.session import RunHookError, SessionContext, SessionError, SessionHooks

CTX = SessionContext(test_id="abc", test_name="cancel order", spec_path="specs/a.highbar.yaml")
REPLY = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    b"X-Padding: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\nContent-Length: 2\r\n\r\n{}"
)


@pytest.fixture
def trickling(request: pytest.FixtureRequest) -> Iterator[str]:
    """A server that sends its reply, status line and headers included, a byte at a time:
    every 0.1 s, or every ``request.param`` seconds when a test parametrizes it."""
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
    yield f"http://127.0.0.1:{server.getsockname()[1]}/sessions"
    stop.set()
    thread.join(timeout=5)
    server.close()


def test_hb39_a_session_hook_with_slow_headers_stops_at_agent_timeout(trickling: str) -> None:
    hooks = SessionHooks(
        SessionConfig(setup=HttpHook(url=trickling)),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=500,
    )
    hooks.load()

    started = time.monotonic()
    with pytest.raises(SessionError, match=r"session\.setup did not finish in 0\.5s"):
        hooks.setup(CTX)

    assert time.monotonic() - started < 1.5


def test_hb39_a_run_hook_with_slow_headers_stops_at_agent_timeout(trickling: str) -> None:
    hooks = SessionHooks(
        SessionConfig(),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=500,
        run=RunConfig(setup=HttpHook(url=trickling)),
    )
    hooks.load()

    started = time.monotonic()
    with pytest.raises(RunHookError, match=r"agent\.run\.setup did not finish in 0\.5s"):
        hooks.before_run()

    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("trickling", [0.005], indirect=True)
def test_hb39_a_hook_that_answers_in_time_still_gets_its_reply(trickling: str) -> None:
    """The same server, with time enough: about 0.6 s of bytes, within 5 s."""
    hooks = SessionHooks(
        SessionConfig(setup=HttpHook(url=trickling)),
        spec_path="specs/a.highbar.yaml",
        timeout_ms=5_000,
    )
    hooks.load()

    assert hooks.setup(CTX) == {}
