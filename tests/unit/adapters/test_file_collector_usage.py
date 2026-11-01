"""The optional `usage` of a `turn` record: contract v1, read or counted as missing."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from highbar.adapters import file_collector
from highbar.adapters.file_collector import FileTraceCollector
from highbar.domain.models import TurnRecord, Usage
from highbar.sdk import trace

FULL = {
    "model": "gpt-4o-mini",
    "provider": "openai",
    "input_tokens": 1000,
    "output_tokens": 200,
    "cached_input_tokens": 800,
    "cost": 0.00021,
    "currency": "USD",
    "llm_calls": 2,
}


def _turn(**fields: Any) -> str:
    record = {"v": 1, "type": "turn", "ts": 1.0, "context": {"test": "t1"}, **fields}
    return json.dumps(record) + "\n"


def _read(path: Path, *lines: str) -> list[TurnRecord]:
    path.write_text("".join(lines), encoding="utf-8")
    return FileTraceCollector(path).collect(since=0).turns


class TestUsage:
    def test_ac1_ac2_a_turn_with_every_field(self, tmp_path: Path) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage=FULL))

        assert turn.usage == Usage(
            model="gpt-4o-mini",
            provider="openai",
            input_tokens=1000,
            output_tokens=200,
            cached_input_tokens=800,
            cost=0.00021,
            currency="USD",
            llm_calls=2,
        )

    def test_ac2_every_field_is_optional(self, tmp_path: Path) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage={"input_tokens": 5}))

        assert turn.usage == Usage(input_tokens=5)

    def test_ac1_a_turn_without_usage_reads_as_it_always_did(self, tmp_path: Path) -> None:
        turns = _read(tmp_path / "t.jsonl", _turn())

        assert turns == [TurnRecord(ts=1.0, context={"test": "t1"})]
        assert turns[0].usage is None

    def test_ac1_the_contract_stays_at_v1(self) -> None:
        assert file_collector.CONTRACT_VERSION == 1
        assert trace.CONTRACT_VERSION == 1

    @pytest.mark.parametrize("value", [None, {}, "lots", 12, [], {"prompt_tokens": 12}])
    def test_ac1_a_usage_with_nothing_to_read_is_no_usage(
        self, tmp_path: Path, value: object
    ) -> None:
        """A writer may have its own `usage`: unknown fields are ignored, as ever."""
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage=value))

        assert turn.usage is None

    @pytest.mark.parametrize(
        "bad", [-1, "1000", True, 10.5, math.nan, math.inf], ids=lambda value: repr(value)
    )
    def test_a_count_that_cannot_be_read_counts_as_missing(
        self, tmp_path: Path, bad: object
    ) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage={"input_tokens": bad, "llm_calls": 1}))

        assert turn.usage == Usage(llm_calls=1)

    @pytest.mark.parametrize(
        "bad",
        [-0.5, "0.5", True, math.nan, math.inf, 10**400],
        ids=["negative", "text", "bool", "nan", "inf", "too long"],
    )
    def test_a_cost_that_cannot_be_read_counts_as_missing(
        self, tmp_path: Path, bad: object
    ) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage={"cost": bad, "llm_calls": 1}))

        assert turn.usage == Usage(llm_calls=1)

    def test_a_whole_count_written_as_a_float_is_a_count(self, tmp_path: Path) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage={"input_tokens": 1000.0}))

        assert turn.usage == Usage(input_tokens=1000)
        assert isinstance(turn.usage.input_tokens, int)

    def test_a_currency_is_one_whatever_its_case(self, tmp_path: Path) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage={"cost": 1, "currency": " usd "}))

        assert turn.usage == Usage(cost=1.0, currency="USD")

    def test_a_label_that_is_not_text_counts_as_missing(self, tmp_path: Path) -> None:
        (turn,) = _read(tmp_path / "t.jsonl", _turn(usage={"model": 4, "provider": "", "cost": 1}))

        assert turn.usage == Usage(cost=1.0)

    def test_ac15_an_llm_record_is_skipped_like_any_unknown_type(self, tmp_path: Path) -> None:
        llm = json.dumps({"v": 1, "type": "llm", "ts": 1.0, "usage": {"input_tokens": 5}}) + "\n"
        path = tmp_path / "t.jsonl"
        path.write_text(llm, encoding="utf-8")

        window = FileTraceCollector(path).collect(since=0)

        assert (window.turns, window.tools) == ([], [])
