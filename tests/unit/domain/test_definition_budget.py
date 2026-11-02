"""AC13: a test without a budget keeps its identity, so old reports still compare.

The two hashes are pinned from before budgets existed: the SHA-256 of each test's
canonical JSON, the form ``definition_sha256`` has always hashed. A test against the
code alone could not tell a hash that changed for every test at once.
"""

from __future__ import annotations

from dataclasses import replace

from highbar.domain.models import Budget, Expect, FunctionCallExpect, ResponseExpect, Turn
from highbar.domain.models import Test as Case

SINGLE = Case(
    trigger="Hi, where is my order 1042?",
    expect=Expect(
        function_calls=[FunctionCallExpect(name="get_order", params={"order_id": "1042"})],
        response=ResponseExpect({"icontains": "thursday"}),
        ordered=True,
    ),
)
CONVERSATION = Case(
    turns=[
        Turn(trigger="my name is Ana", expect=Expect()),
        Turn(
            trigger="what is my name?",
            expect=Expect(response=ResponseExpect({"contains": "Ana"})),
        ),
    ]
)


def test_ac13_a_test_without_a_budget_keeps_its_hash() -> None:
    assert SINGLE.definition_sha256 == (
        "d2d041c5a54a2868388315881692136de990bb2c7f0e052163f7a5d1b9100e52"
    )


def test_ac13_a_conversation_without_a_budget_keeps_its_hash() -> None:
    assert CONVERSATION.definition_sha256 == (
        "c09b4ec8f899c53c170cdcc69c9d340759ef68250e85097c231c308fc739524a"
    )


def test_a_budget_changes_what_counts_as_right() -> None:
    assert SINGLE.expect is not None
    budgeted = replace(SINGLE, expect=replace(SINGLE.expect, budget=Budget(max_input_tokens=1000)))
    other = replace(SINGLE, expect=replace(SINGLE.expect, budget=Budget(max_input_tokens=2000)))

    assert budgeted.definition_sha256 != SINGLE.definition_sha256
    assert budgeted.definition_sha256 != other.definition_sha256


def test_a_budget_on_a_conversation_is_part_of_it() -> None:
    budgeted = replace(CONVERSATION, expect=Expect(budget=Budget(max_cost=1)))

    assert budgeted.definition_sha256 != CONVERSATION.definition_sha256
