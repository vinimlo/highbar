"""HB-3: expect.retrieval against the test's `retrieval` records (decisions on the card)."""

from __future__ import annotations

import pytest

from highbar.assertions.records import RecordNotObserved
from highbar.assertions.retrieval import check_retrieval
from highbar.domain.models import ItemRef, Retrieval, RetrievalExpect, RetrievedItem

WARRANTY = RetrievedItem(id="tr-7", title="Warranty", source="expanded", chunks=(0, 1), tokens=500)
MANUAL = RetrievedItem(id="tr-2", title="Manual", source="retrieved", chunks=(3,), tokens=200)


def _record(*items: RetrievedItem, tokens: int | None = None) -> Retrieval:
    return Retrieval(ts=1.0, items=items, tokens=tokens)


def test_hb3_no_record_is_not_observed_never_a_pass() -> None:
    """`excludes` would pass on an agent that records nothing."""
    with pytest.raises(RecordNotObserved, match="no `retrieval` record"):
        check_retrieval([], RetrievalExpect(excludes=("tr-9",)))


def test_hb3_includes_by_id_or_title_and_source_over_the_tests_records() -> None:
    records = [_record(WARRANTY), _record(MANUAL)]

    assert (
        check_retrieval(records, RetrievalExpect(includes=(ItemRef("tr-7"), ItemRef("Manual"))))
        == []
    )
    assert check_retrieval(records, RetrievalExpect(includes=(ItemRef("tr-7", "sticky"),))) == [
        "retrieval: 'tr-7' did not enter the prompt as sticky (it came as expanded)"
    ]
    assert check_retrieval(records, RetrievalExpect(includes=(ItemRef("tr-9"),))) == [
        "retrieval: 'tr-9' did not enter the prompt (it had tr-7, tr-2)"
    ]


def test_hb3_excludes() -> None:
    assert check_retrieval([_record(WARRANTY)], RetrievalExpect(excludes=("Warranty",))) == [
        "retrieval: 'Warranty' entered the prompt, and must not"
    ]


def test_hb3_max_items_and_max_tokens_hold_for_each_prompt() -> None:
    record = _record(WARRANTY, MANUAL, tokens=900)

    assert check_retrieval([record], RetrievalExpect(max_items=1, max_tokens=800)) == [
        "retrieval: record 1 put 2 items in the prompt, at most 1",
        "retrieval: record 1 put 900 tokens in the prompt, at most 800",
    ]


def test_hb3_tokens_add_up_from_the_items_when_the_record_has_none() -> None:
    assert check_retrieval([_record(WARRANTY, MANUAL)], RetrievalExpect(max_tokens=600)) == [
        "retrieval: record 1 put 700 tokens in the prompt, at most 600"
    ]
    unknown = _record(RetrievedItem(id="x"))
    assert check_retrieval([unknown], RetrievalExpect(max_tokens=600)) == [
        "retrieval: record 1 does not say how many tokens it put in the prompt"
    ]


def test_hb3_chunks_in_order() -> None:
    shuffled = RetrievedItem(id="tr-7", chunks=(2, 0, 1))

    assert check_retrieval([_record(WARRANTY)], RetrievalExpect(chunks_in_order=True)) == []
    assert check_retrieval([_record(shuffled)], RetrievalExpect(chunks_in_order=True)) == [
        "retrieval: the chunks of 'tr-7' entered as [2, 0, 1], not in order"
    ]
