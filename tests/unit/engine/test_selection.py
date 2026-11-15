"""HB-17 v0: run part of the tests, by suite (``--suite``) and by name (``-k``).

A selection that picks no test stops the run before any agent starts: a run that
checked nothing must never read as green.
"""

from __future__ import annotations

import pytest

from highbar.domain.models import AgentConfig, Expect, ResponseExpect, Spec, Suite
from highbar.domain.models import Test as Case
from highbar.engine.selection import SelectionError, select

AGENT = AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat")
OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))


def _spec(suites: dict[str, list[str]]) -> Spec:
    return Spec(
        agent=AGENT,
        suites=[
            Suite(name=name, tests=[Case(trigger=trigger, expect=OK) for trigger in triggers])
            for name, triggers in suites.items()
        ],
    )


ORDERS = _spec({"Orders": ["where is my order", "cancel it"], "Support": ["talk to a human"]})
MENU = _spec({"Menu": ["show the menu"]})
SPECS = [("orders.highbar.yaml", ORDERS), ("menu.highbar.yaml", MENU)]


def _picked(specs: list[tuple[str, Spec]]) -> list[str]:
    return [
        f"{path}:{suite.name}/{test.trigger}"
        for path, spec in specs
        for suite in spec.suites
        for test in suite.tests
    ]


class TestSelect:
    def test_hb17_no_option_runs_everything(self) -> None:
        assert select(SPECS, suites=(), keywords=()) is SPECS

    def test_hb17_a_suite_by_its_name(self) -> None:
        picked = select(SPECS, suites=["Support", "Menu"], keywords=())

        assert _picked(picked) == [
            "orders.highbar.yaml:Support/talk to a human",
            "menu.highbar.yaml:Menu/show the menu",
        ]

    def test_hb17_a_spec_with_nothing_picked_is_left_out(self) -> None:
        picked = select(SPECS, suites=["Orders"], keywords=())

        assert [path for path, _ in picked] == ["orders.highbar.yaml"]

    def test_hb17_k_is_part_of_suite_slash_name_ignoring_case(self) -> None:
        picked = select(SPECS, suites=(), keywords=["ORDERS/cancel"])

        assert _picked(picked) == ["orders.highbar.yaml:Orders/cancel it"]

    def test_hb17_k_reads_a_tests_id_too(self) -> None:
        tagged = Case(trigger="refund it", expect=OK, id="refund-flow")
        spec = Spec(agent=AGENT, suites=[Suite(name="Orders", tests=[tagged])])

        picked = select([("a.highbar.yaml", spec)], suites=(), keywords=["refund-flow"])

        assert picked[0][1].suites[0].tests == [tagged]

    def test_hb17_k_given_twice_picks_either(self) -> None:
        picked = select(SPECS, suites=(), keywords=["human", "menu"])

        assert _picked(picked) == [
            "orders.highbar.yaml:Support/talk to a human",
            "menu.highbar.yaml:Menu/show the menu",
        ]

    def test_hb17_suite_and_k_together_pick_both_ways(self) -> None:
        picked = select(SPECS, suites=["Orders"], keywords=["order"])

        assert _picked(picked) == [
            "orders.highbar.yaml:Orders/where is my order",
            "orders.highbar.yaml:Orders/cancel it",
        ]

    def test_hb17_the_tests_are_the_same_objects(self) -> None:
        """What a test measures, and its definition_sha256, do not change by being picked."""
        (path, spec), *_ = select(SPECS, suites=["Orders"], keywords=())

        assert spec.suites[0].tests[0] is ORDERS.suites[0].tests[0]
        assert spec.agent is ORDERS.agent


class TestNothingPicked:
    def test_hb17_a_suite_no_spec_has_is_named(self) -> None:
        with pytest.raises(SelectionError) as caught:
            select(SPECS, suites=["Order"], keywords=())

        assert str(caught.value) == (
            "--suite 'Order': no spec has a suite with that name — did you mean 'Orders'? "
            "(suites: Menu, Orders, Support)"
        )

    def test_hb17_a_selection_that_picks_no_test_stops_the_run(self) -> None:
        with pytest.raises(SelectionError) as caught:
            select(SPECS, suites=["Support"], keywords=["cancel", "refund"])

        assert str(caught.value) == (
            "no test matches --suite Support -k cancel -k refund: nothing would be checked"
        )


TAGGED = Spec(
    agent=AGENT,
    suites=[
        Suite(
            name="Orders",
            tests=[
                Case(trigger="where is my order", expect=OK, tags=("smoke", "orders")),
                Case(trigger="cancel it", expect=OK, tags=("orders", "slow")),
            ],
        ),
        Suite(name="Menu", tests=[Case(trigger="show the menu", expect=OK)]),
    ],
)


class TestTags:
    """HB-17 v1: ``tags:`` in a suite and a test, picked with ``--tag``."""

    def test_hb17_a_tag_picks_the_tests_that_have_it(self) -> None:
        picked = select([("a.highbar.yaml", TAGGED)], suites=(), keywords=(), tags=["smoke"])

        assert _picked(picked) == ["a.highbar.yaml:Orders/where is my order"]

    def test_hb17_tag_given_twice_picks_either(self) -> None:
        picked = select(
            [("a.highbar.yaml", TAGGED)], suites=(), keywords=(), tags=["smoke", "slow"]
        )

        assert len(_picked(picked)) == 2

    def test_hb17_tag_and_k_together_pick_both_ways(self) -> None:
        picked = select(
            [("a.highbar.yaml", TAGGED)], suites=(), keywords=["cancel"], tags=["orders"]
        )

        assert _picked(picked) == ["a.highbar.yaml:Orders/cancel it"]

    def test_hb17_a_tag_no_test_has_is_named(self) -> None:
        with pytest.raises(SelectionError) as caught:
            select([("a.highbar.yaml", TAGGED)], suites=(), keywords=(), tags=["smok"])

        assert str(caught.value) == (
            "--tag 'smok': no test has that tag — did you mean 'smoke'? (tags: orders, slow, smoke)"
        )

    def test_hb17_with_no_tags_anywhere_it_says_so(self) -> None:
        with pytest.raises(SelectionError, match=r"\(tags: none\)$"):
            select(SPECS, suites=(), keywords=(), tags=["smoke"])
