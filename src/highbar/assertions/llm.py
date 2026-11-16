"""``expect.llm`` (HB-11): the test's calls to the model, from its `llm` records.

The semantics are decision E4 on the card; E2: with no `llm` record there is nothing to
check, and that is never a pass.
"""

from __future__ import annotations

from highbar.assertions._match import value_match
from highbar.domain.models import LlmCall, LlmExpect


class LlmNotObserved(Exception):
    """No `llm` record for the test: its expect.llm could not be checked (an `error`)."""


def check_llm(calls: list[LlmCall], expect: LlmExpect) -> list[str]:
    """The failures of ``expect`` against ``calls``, in the order they were recorded."""
    if not calls:
        raise LlmNotObserved(
            "expect.llm: no `llm` record for this test, so its calls to the model were not "
            "checked; record each call with hb.llm(...) or an `llm` line in the trace"
        )
    failures: list[str] = []
    if expect.model is not None:
        answered = [(n, call) for n, call in enumerate(calls, 1) if call.error is None]
        if not answered:
            failures.append(f"llm: every one of the {len(calls)} calls failed: none answered")
        elif (model := answered[-1][1].model) is None:
            # Matched as "", it would pass every not_contains (review of #67).
            failures.append(
                f"llm: call {answered[-1][0]} answered without saying its model, "
                "so it cannot be checked"
            )
        else:
            for key, operand in expect.model.items():
                if not value_match({key: operand}, model):
                    failures.append(
                        f"llm: the model that answered is {model!r}, expected {key} {operand!r}"
                    )
    if expect.fallback is not None:
        fell = _fallback(calls)
        if expect.fallback and fell is None:
            failures.append("llm: no fallback happened, expected one")
        elif not expect.fallback and fell is not None:
            failures.append(f"llm: a fallback happened ({fell}), expected none")
    if expect.last_role is not None:
        for n, call in enumerate(calls, 1):
            if not call.roles:
                failures.append(f"llm: call {n} has no roles, so its last role cannot be checked")
            elif call.roles[-1] != expect.last_role:
                failures.append(
                    f"llm: call {n} ended on a {call.roles[-1]!r} message, "
                    f"expected {expect.last_role!r}"
                )
    if expect.max_calls is not None and len(calls) > expect.max_calls:
        failures.append(f"llm: {len(calls)} calls to the model, at most {expect.max_calls}")
    return failures


def _fallback(calls: list[LlmCall]) -> str | None:
    """The first sign of a fallback or a retry, said as a clause, or None.

    A call after one that failed is one, whatever its fields say: an agent that does not
    set ``fallback_from`` or ``attempt`` still fell back (review of #67).
    """
    for n, call in enumerate(calls, 1):
        if call.fallback_from:
            return f"call {n} came from {call.fallback_from!r}"
        if (call.attempt or 1) > 1:
            return f"call {n} was attempt {call.attempt}"
        if n > 1 and calls[n - 2].error is not None:
            return f"call {n} came after call {n - 1} failed"
    return None
