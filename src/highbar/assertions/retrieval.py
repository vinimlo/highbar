"""``expect.retrieval`` (HB-3): what entered the prompt, from the test's `retrieval` records.

``includes`` and ``excludes`` read every record of the test; ``max_items`` and
``max_tokens`` hold for each one, a prompt each; ``chunks_in_order`` for each document.
"""

from __future__ import annotations

from highbar.assertions.records import RecordNotObserved
from highbar.domain.models import ItemRef, Retrieval, RetrievalExpect, RetrievedItem


def check_retrieval(records: list[Retrieval], expect: RetrievalExpect) -> list[str]:
    """The failures of ``expect`` against ``records``, in the order they were written."""
    if not records:
        raise RecordNotObserved(
            "expect.retrieval: no `retrieval` record for this test, so what entered the "
            "prompt was not checked; record it with hb.retrieval(...) or a `retrieval` line"
        )
    items = [item for record in records for item in record.items]
    failures = [message for ref in expect.includes if (message := _missing(ref, items))]
    for key in expect.excludes:
        if any(_named(item, key) for item in items):
            failures.append(f"retrieval: {key!r} entered the prompt, and must not")
    for n, record in enumerate(records, 1):
        if expect.max_items is not None and len(record.items) > expect.max_items:
            failures.append(
                f"retrieval: record {n} put {len(record.items)} items in the prompt, "
                f"at most {expect.max_items}"
            )
        if expect.max_tokens is not None:
            tokens = _tokens(record)
            if tokens is None:
                failures.append(
                    f"retrieval: record {n} does not say how many tokens it put in the prompt"
                )
            elif tokens > expect.max_tokens:
                failures.append(
                    f"retrieval: record {n} put {tokens} tokens in the prompt, "
                    f"at most {expect.max_tokens}"
                )
    if expect.chunks_in_order:
        for item in items:
            if list(item.chunks) != sorted(item.chunks):
                failures.append(
                    f"retrieval: the chunks of {_label(item)!r} entered as "
                    f"{list(item.chunks)}, not in order"
                )
    return failures


def _missing(ref: ItemRef, items: list[RetrievedItem]) -> str | None:
    named = [item for item in items if _named(item, ref.key)]
    if not named:
        seen = ", ".join(_label(item) for item in items) or "nothing"
        return f"retrieval: {ref.key!r} did not enter the prompt (it had {seen})"
    if ref.source is not None and all(item.source != ref.source for item in named):
        came = ", ".join(sorted({item.source or "no source" for item in named}))
        return (
            f"retrieval: {ref.key!r} did not enter the prompt as {ref.source} (it came as {came})"
        )
    return None


def _named(item: RetrievedItem, key: str) -> bool:
    return key in (item.id, item.title)


def _label(item: RetrievedItem) -> str:
    return item.id or item.title or "?"


def _tokens(record: Retrieval) -> int | None:
    """The record's own count, or its documents' added up when every one has a count."""
    if record.tokens is not None:
        return record.tokens
    counts = [item.tokens for item in record.items]
    if not counts or any(count is None for count in counts):
        return None
    return sum(count for count in counts if count is not None)
