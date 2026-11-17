"""The W3C traceparent highbar sends with each message when it reads OTLP (HB-48).

Its trace id is the test execution's id, so a span that carries it is that test's.
"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest

from highbar.adapters.http_client import HTTPClient
from highbar.domain.models import RequestConfig
from highbar.engine.placeholders import Values

ENDPOINT = "http://agent.test/chat"
# version 00, a 16-byte trace id, an 8-byte parent id, flags 01 (sampled).
TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-01$")


def _send(
    test_id: str, *, traceparent: bool, request: RequestConfig | None = None
) -> httpx.Request:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"response": "ok"})

    client = HTTPClient(
        ENDPOINT,
        request=request,
        transport=httpx.MockTransport(handler),
        traceparent=traceparent,
    )
    client.send("hi", values=Values(message="hi", test_id=test_id, test_name="t", session={}))
    return sent[0]


def test_traceparent_is_valid_w3c_and_carries_the_test_id() -> None:
    test_id = uuid.uuid4().hex

    request = _send(test_id, traceparent=True)

    match = TRACEPARENT.match(request.headers["traceparent"])
    assert match is not None, request.headers["traceparent"]
    assert match.group(1) == test_id
    assert match.group(2) != "0" * 16
    assert request.headers["x-highbar-test"] == test_id


def test_each_execution_sends_its_own_traceparent() -> None:
    first = _send(uuid.uuid4().hex, traceparent=True).headers["traceparent"]
    second = _send(uuid.uuid4().hex, traceparent=True).headers["traceparent"]

    assert first.split("-")[1] != second.split("-")[1]
    assert first.split("-")[2] != second.split("-")[2]


def test_each_message_gets_a_fresh_span_id() -> None:
    test_id = uuid.uuid4().hex

    first = _send(test_id, traceparent=True).headers["traceparent"]
    second = _send(test_id, traceparent=True).headers["traceparent"]

    assert first.split("-")[2] != second.split("-")[2]


def test_a_traceparent_in_the_spec_is_replaced() -> None:
    test_id = uuid.uuid4().hex
    spoofed = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"

    request = _send(
        test_id, traceparent=True, request=RequestConfig(headers={"TraceParent": spoofed})
    )

    assert request.headers.get_list("traceparent") == [request.headers["traceparent"]]
    assert request.headers["traceparent"].split("-")[1] == test_id


def test_no_traceparent_for_highbars_own_contract() -> None:
    assert "traceparent" not in _send(uuid.uuid4().hex, traceparent=False).headers


def test_a_test_id_that_is_not_a_trace_id_is_refused() -> None:
    with pytest.raises(ValueError, match="trace id"):
        _send("t-1", traceparent=True)
