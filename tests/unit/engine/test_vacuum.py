"""HB-47: a test that an agent doing nothing useful passes says nothing about the agent.

Each test is judged against built-in trivial agents, with the assertions a run uses:
silent (no reply, no call), echo (the message back), every tool (each tool the spec
names, with no arguments) and every option (one reply naming every text the spec's
replies look for).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec
from highbar.engine.vacuum import probe_spec


def _probe(tmp_path: Path, tests: str) -> dict[str, tuple[str, ...]]:
    spec = tmp_path / "s.highbar.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            agent:
              start: "true"
              endpoint: "http://localhost:8000/chat"
            suites:
              - name: S
                tests:
            """
        )
        + textwrap.indent(textwrap.dedent(tests), " " * 6)
    )
    return {f.test: f.passes_on for f in probe_spec(parse_spec(str(spec)))}


def test_hb47_a_negative_reply_check_passes_on_silence(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: never says error
          trigger: "Where is my order?"
          expect:
            response: {not_contains: "error"}
        """,
    )
    assert "silent" in found["never says error"]


def test_hb47_a_guardrail_alone_passes_on_doing_nothing(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: does not delete
          trigger: "delete my account"
          expect:
            function_calls: []
        """,
    )
    assert set(found["does not delete"]) >= {"silent", "echo"}


def test_hb47_a_reply_that_lists_everything_passes_a_contains(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: in transit
          trigger: "Where is order 1042?"
          expect:
            response: {contains: "in transit"}
        - name: delivered
          trigger: "Where is order 1043?"
          expect:
            response: {contains: "delivered", not_contains: "in transit"}
        """,
    )
    assert found["in transit"] == ("every option",)
    assert found["delivered"] == ()


def test_hb47_echo_passes_a_check_the_message_already_holds(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: greets
          trigger: "say hello to Ana"
          expect:
            response: {icontains: "ana"}
        """,
    )
    assert "echo" in found["greets"]


def test_hb47_a_bare_call_check_passes_on_calling_every_tool(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: searches
          trigger: "find a dress"
          expect:
            function_calls:
              - name: search
        - name: searches for a dress
          trigger: "find a dress"
          expect:
            function_calls:
              - name: search
                params: {query: {icontains: "dress"}}
        - name: never books unasked
          trigger: "hi"
          expect:
            forbid: [book]
            response: {icontains: "hello"}
        """,
    )
    # The spec names a second tool, so "every tool" does more than the right answer.
    assert found["searches"] == ("every tool",)
    assert found["searches for a dress"] == ()


def test_hb47_a_strong_test_passes_on_none(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: looks the order up
          trigger: "Where is order 1042?"
          expect:
            function_calls:
              - name: get_order
                params: {order_id: "1042"}
            response: {contains: "in transit"}
        """,
    )
    assert found["looks the order up"] == ()


def test_hb47_every_turn_must_pass_on_the_same_agent(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: two turns
          turns:
            - trigger: "hi"
              expect:
                response: {icontains: "hi"}
            - trigger: "find a dress"
              expect:
                function_calls:
                  - name: search
        """,
    )
    # Echo holds the first turn and calling every tool the second; neither holds both.
    assert found["two turns"] == ()


@pytest.mark.parametrize(
    "expect",
    [
        'check: {python: "checks:db"}',
        "llm: {max_calls: 3}",
        "retrieval: {excludes: [draft]}",
    ],
)
def test_hb47_what_a_trivial_agent_cannot_satisfy_is_not_vacuous(
    tmp_path: Path, expect: str
) -> None:
    found = _probe(
        tmp_path,
        f"""
        - name: t
          trigger: "x"
          expect:
            response: {{not_contains: "error"}}
            {expect}
        """,
    )
    assert found["t"] == ()


def test_hb47_a_negative_control_is_left_out(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: control
          trigger: "x"
          expect_status: failed
          expect:
            response: {not_contains: "error"}
        """,
    )
    assert "control" not in found


def test_review_76_the_right_answer_alone_is_not_every_option(tmp_path: Path) -> None:
    # Review of #76: in a one-test spec, "every option" replied with the test's own
    # text, which is the right answer, and the test was marked vacuous.
    found = _probe(
        tmp_path,
        """
        - name: capital
          trigger: "capital of France?"
          expect:
            response: {contains: "Paris"}
        """,
    )
    assert found["capital"] == ()


def test_review_76_naming_another_tests_answer_too_still_counts(tmp_path: Path) -> None:
    found = _probe(
        tmp_path,
        """
        - name: France
          trigger: "capital of France?"
          expect:
            response: {contains: "Paris"}
        - name: Italy
          trigger: "capital of Italy?"
          expect:
            response: {contains: "Rome"}
        """,
    )
    assert found["France"] == ("every option",)
    assert found["Italy"] == ("every option",)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ('{contains: "Paris"}', '{contains: "Paris is the capital"}'),
        ('{icontains: "paris"}', '{contains: "Paris"}'),
    ],
)
def test_review_76_the_same_answer_written_two_ways_is_not_an_option(
    tmp_path: Path, first: str, second: str
) -> None:
    # Re-review of #76: an exact-string "own text" check flagged both tests when two
    # of them asked for the same answer in other words.
    found = _probe(
        tmp_path,
        f"""
        - name: one
          trigger: "capital of France?"
          expect:
            response: {first}
        - name: two
          trigger: "capital of France, please"
          expect:
            response: {second}
        """,
    )
    assert found == {"one": (), "two": ()}


def test_hb47_every_option_names_only_the_answers_of_the_tests_suite(tmp_path: Path) -> None:
    # The options a reply could list are the answers to related questions: the tests
    # of one suite. A guardrail's "can't" is not beaten by an order's "in transit".
    spec = tmp_path / "s.highbar.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            agent:
              start: "true"
              endpoint: "http://localhost:8000/chat"
            suites:
              - name: Orders
                tests:
                  - name: in transit
                    trigger: "Where is order 1042?"
                    expect:
                      response: {contains: "in transit"}
              - name: Guardrails
                tests:
                  - name: refuses
                    trigger: "delete my account"
                    expect:
                      forbid: [delete_account]
                      response: {icontains: "can't"}
            """
        )
    )
    found = {f.test: f.passes_on for f in probe_spec(parse_spec(str(spec)))}
    assert found == {"in transit": (), "refuses": ()}


def test_hb47_an_order_by_name_only_is_vacuous_whichever_order_the_spec_names(
    tmp_path: Path,
) -> None:
    # Review of #76: "every tool" calls in spec order, so a name-only `ordered` test
    # that expects the other order was missed.
    found = _probe(
        tmp_path,
        """
        - name: forbids a
          trigger: "x"
          expect:
            forbid: [a, z]
            response: {contains: "x"}
        - name: b then a
          trigger: "y"
          expect:
            ordered: true
            function_calls:
              - name: b
              - name: a
        """,
    )
    # The spec names a first, and z, which the test does not expect; "every tool" calls
    # the test's own tools first, in its order, and z after, so the test, which does
    # not rule out other calls, holds on more than the right answer.
    assert "every tool" in found["b then a"]


def test_review_80_an_exclusive_call_by_name_alone_can_be_the_whole_answer(
    tmp_path: Path,
) -> None:
    # Review of #80: "only X" flagged exclusive [transfer_to_human], the right answer
    # for a tool that takes nothing. Calling exactly what the test expects is no vacuum.
    found = _probe(
        tmp_path,
        """
        - name: hands over
          trigger: "I want a person"
          expect:
            exclusive: true
            function_calls:
              - name: transfer_to_human
        - name: books
          trigger: "book it"
          expect:
            function_calls:
              - name: book
                params: {id: "7"}
        """,
    )
    assert found["hands over"] == ()
    assert found["books"] == ()


@pytest.mark.parametrize("count", [3, 4])
def test_review_80_a_long_order_by_name_only_is_vacuous(tmp_path: Path, count: int) -> None:
    # "every tool, reversed" covered two tools only: with three, the spec's order and
    # its reverse both miss c, a, b.
    names = ["a", "b", "c", "d"][:count]
    lines: list[str] = []
    for name in [*names, "z"]:  # the spec names them in this order first, z unexpected
        lines += [f"- name: forbids {name}", "  trigger: x", "  expect:"]
        lines += [f"    forbid: [{name}]", "    response: {contains: x}"]
    lines += ["- name: in order", "  trigger: y", "  expect:", "    ordered: true"]
    lines += ["    function_calls:", *(f"      - name: {n}" for n in names[2:] + names[:2])]
    found = _probe(tmp_path, "\n".join(lines) + "\n")
    assert "every tool" in found["in order"]


@pytest.mark.parametrize(
    "tests",
    [
        # Y1: the spec's only tool, alone.
        """
        - name: hands over
          trigger: "I want a person"
          expect:
            exclusive: true
            function_calls:
              - name: transfer_to_human
        """,
        # Y2: every tool the spec names, in the test's order.
        """
        - name: hands over
          trigger: "I want a person"
          expect:
            exclusive: true
            ordered: true
            function_calls:
              - name: b
              - name: a
        """,
    ],
)
def test_review_80_every_tool_that_adds_nothing_is_the_right_answer(
    tmp_path: Path, tests: str
) -> None:
    # Re-review of #80: when the test expects every tool the spec names, "every tool"
    # calls exactly the right answer, which is no vacuum.
    assert _probe(tmp_path, tests)["hands over"] == ()
