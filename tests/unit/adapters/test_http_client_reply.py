"""HB-14: with agent.reply: turn, the HTTP body is not the reply.

A webhook answers 202 with nothing, or with a status of its own: neither is an error,
since the reply comes later in the turn record. A status that is not 2xx still is.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from highbar.adapters.http_client import AgentResponseError, HTTPClient
from highbar.engine.placeholders import Values

ENDPOINT = "http://agent.test/chat"
VALUES = Values(message="hi", test_id="t-1", test_name="greeting", session={})


def _client(response: httpx.Response, **kwargs: Any) -> HTTPClient:
    return HTTPClient(ENDPOINT, transport=httpx.MockTransport(lambda _: response), **kwargs)


@pytest.mark.parametrize(
    "response",
    [httpx.Response(202), httpx.Response(202, json={"status": "queued"})],
    ids=["empty", "a status of its own"],
)
def test_hb14_a_webhook_that_answers_later_is_not_an_error(response: httpx.Response) -> None:
    assert _client(response, body_is_reply=False).send("hi", values=VALUES) == ""


def test_hb14_a_status_that_is_not_2xx_still_is() -> None:
    with pytest.raises(AgentResponseError, match="500"):
        _client(httpx.Response(500, text="down"), body_is_reply=False).send("hi", values=VALUES)
