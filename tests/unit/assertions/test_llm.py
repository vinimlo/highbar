"""HB-11: expect.llm against the test's `llm` records (decisions E2 and E4, on the card)."""

from __future__ import annotations

import pytest

from highbar.assertions.llm import LlmNotObserved, check_llm
from highbar.domain.models import LlmCall, LlmExpect


def _call(model: str = "gpt-4o-mini", **fields: object) -> LlmCall:
    return LlmCall(ts=1.0, model=model, roles=("system", "user", "system"), **fields)  # type: ignore[arg-type]


def test_hb11_no_llm_record_is_not_observed_never_a_pass() -> None:
    """`fallback: false` would pass on an agent that writes nothing (E2)."""
    with pytest.raises(LlmNotObserved, match="no `llm` record"):
        check_llm([], LlmExpect(fallback=False))


def test_hb11_model_is_the_call_that_answered() -> None:
    calls = [_call("gpt-4o-mini", error="rate limited"), _call("deepseek-flash", attempt=2)]

    assert check_llm(calls, LlmExpect(model={"equals": "deepseek-flash"})) == []
    assert check_llm(calls, LlmExpect(model={"equals": "gpt-4o-mini"})) == [
        "llm: the model that answered is 'deepseek-flash', expected equals 'gpt-4o-mini'"
    ]


def test_hb11_fallback_is_any_call_from_another_model_or_a_retry() -> None:
    plain = [_call()]
    fell_back = [_call(error="down"), _call("deepseek-flash", fallback_from="gpt-4o-mini")]

    assert check_llm(plain, LlmExpect(fallback=False)) == []
    assert check_llm(fell_back, LlmExpect(fallback=True)) == []
    assert check_llm(fell_back, LlmExpect(fallback=False)) == [
        "llm: a fallback happened (call 2 came from 'gpt-4o-mini'), expected none"
    ]
    assert check_llm(plain, LlmExpect(fallback=True)) == ["llm: no fallback happened, expected one"]


def test_hb11_last_role_holds_for_every_call_and_names_the_one_that_broke_it() -> None:
    calls = [_call(), LlmCall(ts=2.0, model="m", roles=("system", "user"))]

    assert check_llm(calls, LlmExpect(last_role="system")) == [
        "llm: call 2 ended on a 'user' message, expected 'system'"
    ]


def test_hb11_a_call_without_roles_cannot_show_its_last_role() -> None:
    assert check_llm([LlmCall(ts=1.0)], LlmExpect(last_role="system")) == [
        "llm: call 1 has no roles, so its last role cannot be checked"
    ]


def test_hb11_max_calls_counts_the_failed_ones_too() -> None:
    calls = [_call(error="down"), _call()]

    assert check_llm(calls, LlmExpect(max_calls=2)) == []
    assert check_llm(calls, LlmExpect(max_calls=1)) == ["llm: 2 calls to the model, at most 1"]
