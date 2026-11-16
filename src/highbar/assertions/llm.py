"""``expect.llm`` (HB-11): the test's calls to the model, from its `llm` records.

The semantics are decision E4 on the card; E2: with no `llm` record there is nothing to
check, and that is never a pass.
"""

from __future__ import annotations

from highbar.assertions._match import value_match
from highbar.assertions.records import RecordNotObserved
from highbar.domain.models import LlmCall, LlmExpect


class LlmNotObserved(RecordNotObserved):
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
        answered = next((call for call in reversed(calls) if call.error is None), None)
        if answered is None:
            failures.append(f"llm: every one of the {len(calls)} calls failed: none answered")
        else:
            for key, operand in expect.model.items():
                if not value_match({key: operand}, answered.model or ""):
                    failures.append(
                        f"llm: the model that answered is {answered.model!r}, "
                        f"expected {key} {operand!r}"
                    )
    if expect.fallback is not None:
        fell = next(
            (
                (n, call)
                for n, call in enumerate(calls, 1)
                if call.fallback_from or (call.attempt or 1) > 1
            ),
            None,
        )
        if expect.fallback and fell is None:
            failures.append("llm: no fallback happened, expected one")
        elif not expect.fallback and fell is not None:
            n, call = fell
            how = (
                f"came from {call.fallback_from!r}"
                if call.fallback_from
                else f"was attempt {call.attempt}"
            )
            failures.append(f"llm: a fallback happened (call {n} {how}), expected none")
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
