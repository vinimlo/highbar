"""HB-3: the `retrieval` record, what entered the prompt for one message."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highbar import Highbar
from highbar.adapters.file_collector import FileTraceCollector, TraceFormatError
from highbar.domain.models import Retrieval, RetrievedItem


def _write(path: Path, *records: dict[str, Any]) -> FileTraceCollector:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return FileTraceCollector(path)


RECORD = {
    "v": 1,
    "type": "retrieval",
    "ts": 2.0,
    "context": {"test": "t-1"},
    "query": "and the deadline?",
    "strategy": "sticky",
    "items": [
        {
            "id": "tr-7",
            "title": "Warranty",
            "source": "expanded",
            "chunks": [0, 1, 2],
            "tokens": 900,
        },
        {"id": "tr-2", "source": "sticky", "chunks": [4]},
    ],
    "tokens": 1200,
}


def test_hb3_a_retrieval_record_is_read_into_the_window(tmp_path: Path) -> None:
    (record,) = _write(tmp_path / "t.jsonl", RECORD).collect(since=0).retrievals

    assert record == Retrieval(
        ts=2.0,
        context={"test": "t-1"},
        query="and the deadline?",
        strategy="sticky",
        items=(
            RetrievedItem(
                id="tr-7", title="Warranty", source="expanded", chunks=(0, 1, 2), tokens=900
            ),
            RetrievedItem(id="tr-2", source="sticky", chunks=(4,)),
        ),
        tokens=1200,
    )


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ("tr-7", "'items' must be a list of objects"),
        ([{"id": "tr-7", "chunks": "0,1"}], r"items\[0\]\.chunks must be a list of whole numbers"),
        ([{"chunks": [0]}], r"items\[0\] has neither 'id' nor 'title'"),
    ],
)
def test_hb3_items_a_check_reads_must_be_readable(tmp_path: Path, items: Any, message: str) -> None:
    collector = _write(tmp_path / "t.jsonl", {**RECORD, "items": items})

    with pytest.raises(TraceFormatError, match=message):
        collector.collect(since=0)


def test_hb3_the_sdk_writes_a_record_the_reader_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("HIGHBAR_TRACES_URL", str(path))

    Highbar().retrieval(items=[{"id": "tr-7", "chunks": [0, 1]}], tokens=300, test="t-1")

    (record,) = FileTraceCollector(path).collect(since=0).retrievals
    assert record.items == (RetrievedItem(id="tr-7", chunks=(0, 1)),)
    assert record.tokens == 300
    assert record.context == {"test": "t-1"}
