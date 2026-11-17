"""HB-11: the `llm` record, one per call to the model, written by the agent or the SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highbar import Highbar
from highbar.adapters.file_collector import FileTraceCollector, TraceFormatError
from highbar.domain.models import LlmCall


def _write(path: Path, *records: dict[str, Any]) -> FileTraceCollector:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return FileTraceCollector(path)


CALL = {
    "v": 1,
    "type": "llm",
    "ts": 2.0,
    "context": {"test": "t-1"},
    "model": "deepseek-flash",
    "provider": "openrouter",
    "attempt": 2,
    "fallback_from": "gpt-4o-mini",
    "roles": ["system", "user", "assistant", "system"],
    "input_tokens": 900,
    "output_tokens": 40,
    "cache_tokens": 512,
    "latency_ms": 830.5,
}


def test_hb11_an_llm_record_is_read_into_the_window(tmp_path: Path) -> None:
    window = _write(tmp_path / "t.jsonl", CALL).collect(since=0)

    assert window.llms == [
        LlmCall(
            ts=2.0,
            context={"test": "t-1"},
            model="deepseek-flash",
            provider="openrouter",
            attempt=2,
            fallback_from="gpt-4o-mini",
            roles=("system", "user", "assistant", "system"),
            input_tokens=900,
            output_tokens=40,
            cache_tokens=512,
            latency_ms=830.5,
        )
    ]
    assert window.tools == [] and window.turns == []


def test_hb11_roles_that_are_not_a_list_of_strings_is_a_bad_line(tmp_path: Path) -> None:
    collector = _write(tmp_path / "t.jsonl", {**CALL, "roles": "system,user"})

    with pytest.raises(TraceFormatError, match="line 1: 'roles' must be a list of role names"):
        collector.collect(since=0)


def test_hb11_the_sdk_writes_a_call_the_reader_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("HIGHBAR_TRACES_URL", str(path))
    hb = Highbar()

    hb.llm(
        model="gpt-4o-mini",
        roles=["system", "user", "system"],
        error="timeout",
        input_tokens=10,
        test="t-1",
    )

    (call,) = FileTraceCollector(path).collect(since=0).llms
    assert call.model == "gpt-4o-mini"
    assert call.roles == ("system", "user", "system")
    assert call.error == "timeout"
    assert call.input_tokens == 10
    assert call.context == {"test": "t-1"}


def test_hb30_a_proxy_record_says_its_source_and_the_cost_it_read(tmp_path: Path) -> None:
    record = {
        **CALL,
        "source": "proxy",
        "read": True,
        "cost": 0.0021,
        "currency": "usd",
    }

    (call,) = _write(tmp_path / "t.jsonl", record).collect(since=0).llms

    assert call.source == "proxy"
    assert call.read is True
    assert call.unread is None
    assert call.cost == 0.0021
    assert call.currency == "USD"


def test_hb30_an_unread_call_keeps_its_reason_and_no_tokens(tmp_path: Path) -> None:
    record = {
        "v": 1,
        "type": "llm",
        "ts": 2.0,
        "source": "proxy",
        "read": False,
        "unread": "the stream ended without usage",
        "model": "gpt-4o-mini",
    }

    (call,) = _write(tmp_path / "t.jsonl", record).collect(since=0).llms

    assert call.read is False
    assert call.unread == "the stream ended without usage"
    # Unknown is not zero: nothing was read, so nothing is there.
    assert call.input_tokens is None and call.output_tokens is None and call.cost is None


def test_hb30_a_record_without_source_or_read_reads_as_before(tmp_path: Path) -> None:
    (call,) = _write(tmp_path / "t.jsonl", CALL).collect(since=0).llms

    assert call.source is None and call.read is None and call.unread is None
    assert call.cost is None and call.currency is None


def test_hb30_a_read_that_is_not_a_boolean_is_a_bad_line(tmp_path: Path) -> None:
    # "no" taken as true would vouch for a count nobody read.
    collector = _write(tmp_path / "t.jsonl", {**CALL, "read": "no"})

    with pytest.raises(TraceFormatError, match="line 1: 'read' must be true or false"):
        collector.collect(since=0)


def test_hb30_the_call_kind_is_read(tmp_path: Path) -> None:
    (call,) = _write(tmp_path / "t.jsonl", {**CALL, "call": "model"}).collect(since=0).llms

    assert call.call == "model"
