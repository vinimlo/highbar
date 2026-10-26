"""ASGI middleware: a `turn` record per HTTP request, tagged with ``X-Highbar-Test``.

    app.add_middleware(highbar.asgi.Middleware, hb=hb)

Tool calls made while the request is handled carry the request's ``test`` in their
context, so highbar can tell them apart from a late call of the previous test (A4).
Standard library only, like the rest of the SDK.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from highbar.sdk import context
from highbar.sdk.core import Highbar

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_TEST_HEADER = b"x-highbar-test"


class Middleware:
    """Wraps an ASGI app (FastAPI, Starlette...). Does nothing while tracing is off."""

    def __init__(self, app: ASGIApp, hb: Highbar) -> None:
        self._app = app
        self._hb = hb

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._hb.enabled:
            await self._app(scope, receive, send)
            return

        written = False

        with context.pushed({"test": _test_id(scope)}) as turn_context:

            async def send_and_record(message: Message) -> None:
                nonlocal written
                # The turn goes in before the last piece of the body: highbar reads the
                # trace file as soon as the reply arrives, and must find it there.
                if (
                    not written
                    and message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                ):
                    written = True
                    self._hb._write_turn(turn_context)  # pyright: ignore[reportPrivateUsage]  # the SDK's own
                await send(message)

            try:
                await self._app(scope, receive, send_and_record)
            finally:
                if not written:
                    # The handler raised, or never answered: the message was still received.
                    self._hb._write_turn(turn_context)  # pyright: ignore[reportPrivateUsage]  # the SDK's own


def _test_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == _TEST_HEADER:
            return value.decode("latin-1")
    return None
