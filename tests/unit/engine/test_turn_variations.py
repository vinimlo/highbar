"""HB-16 v0: `variations` on the last turn of a multi-turn test.

The elliptic follow-up ("and the deadline?") is where a conversation breaks, and it is
the turn that needs paraphrases. Only that turn's trigger changes; the turns before it
are sent as written, so the conversation each paraphrase answers is the same one.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Expect,
    ResponseExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    Turn,
)
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.schema import SpecError
from tests.unit.engine.fakes import FakeAgent, observed
from tests.unit.engine.test_outcomes import Replying

SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))


def _spec(tmp_path: Path, test: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        'agent:\n  endpoint: "http://localhost:8000/chat"\n'
        "suites:\n  - name: S\n    tests:\n" + textwrap.indent(textwrap.dedent(test), "      "),
        encoding="utf-8",
    )
    return str(path)


TWO_TURNS = """\
- name: deadline
  turns:
    - trigger: "I want to cancel my training"
    - trigger: "and the deadline?"
      variations: 3
      expect: {response: {contains: "days"}}
"""


class TestSpec:
    def test_hb16_the_last_turn_can_ask_for_variations(self, tmp_path: Path) -> None:
        spec = parse_spec(_spec(tmp_path, TWO_TURNS))

        (test,) = spec.suites[0].tests
        assert test.variations == 3
        assert [turn.trigger for turn in test.turns or []] == [
            "I want to cancel my training",
            "and the deadline?",
        ]

    def test_hb16_an_earlier_turn_cannot_yet(self, tmp_path: Path) -> None:
        with pytest.raises(
            SpecError,
            match=r"tests\[0\]\.turns\[0\]: 'variations' goes on the last turn only",
        ):
            parse_spec(
                _spec(
                    tmp_path,
                    """\
                    - turns:
                        - trigger: "I want to cancel my training"
                          variations: 3
                        - trigger: "and the deadline?"
                          expect: {response: {contains: "days"}}
                    """,
                )
            )

    def test_hb16_the_test_level_key_points_to_the_turn(self, tmp_path: Path) -> None:
        with pytest.raises(
            SpecError, match=r"tests\[0\]: on a multi-turn test, 'variations' goes on its last turn"
        ):
            parse_spec(
                _spec(
                    tmp_path,
                    """\
                    - variations: 3
                      turns:
                        - trigger: "I want to cancel my training"
                        - trigger: "and the deadline?"
                          expect: {response: {contains: "days"}}
                    """,
                )
            )

    @pytest.mark.parametrize(
        "template", ["{{previous.response}}", "{{previous.turns[0].response}}"]
    )
    def test_hb16_a_last_turn_with_a_template_cannot_have_variations(
        self, tmp_path: Path, template: str
    ) -> None:
        """A paraphrase can drop the placeholder or change it (as HB-20 says)."""
        with pytest.raises(SpecError, match=r"cannot have variations"):
            parse_spec(
                _spec(
                    tmp_path,
                    f"""\
                    - turns:
                        - trigger: "I want to cancel my training"
                        - trigger: "and {template}?"
                          variations: 3
                          expect: {{response: {{contains: "days"}}}}
                    """,
                )
            )


class _Generator:
    """Paraphrases of what it is asked, and what it was asked."""

    def __init__(self, paraphrases: list[str]) -> None:
        self._paraphrases = paraphrases
        self.asked: list[tuple[str, int, tuple[str, ...]]] = []

    def generate(self, trigger: str, count: int, context: Sequence[str] = ()) -> list[str]:
        self.asked.append((trigger, count, tuple(context)))
        return self._paraphrases


def _run_conversation(
    generator: _Generator, agent: FakeAgent, expect: Expect, reporter: Any = None
) -> RunSummary:
    test = Case(
        name="deadline",
        turns=[
            Turn(trigger="I want to cancel my training", expect=Expect()),
            Turn(trigger="and the deadline?", expect=expect),
        ],
        variations=2,
    )
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        variations=generator,
    ).run(Spec(agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"), suites=[Suite("S", [test])]))


class TestRunner:
    def test_hb16_the_title_says_how_many_variations_ran(self) -> None:
        reporter = MagicMock()

        _run_conversation(
            _Generator(["what about the deadline?", "until when?"]),
            FakeAgent(observed),
            SAYS_OK,
            reporter,
        )

        name, _ = reporter.on_test_result.call_args[0]
        assert name == "deadline (2 variations)"

    def test_hb16_each_paraphrase_replaces_only_the_last_turn(self) -> None:
        generator = _Generator(["what about the deadline?", "until when?"])
        agent = FakeAgent(observed)

        summary = _run_conversation(generator, agent, SAYS_OK)

        assert [message for message, _ in agent.sent] == [
            "I want to cancel my training",
            "and the deadline?",
            "I want to cancel my training",
            "what about the deadline?",
            "I want to cancel my training",
            "until when?",
        ]
        (test,) = summary.tests
        assert test.status is Status.PASSED
        assert [(p.kind, p.text) for p in test.phrasings] == [
            ("conversation", None),
            ("variation", "what about the deadline?"),
            ("variation", "until when?"),
        ]

    def test_hb16_the_generator_sees_the_conversation_before_the_turn(self) -> None:
        """An elliptic follow-up means nothing alone: "and the deadline?" of what."""
        generator = _Generator(["what about the deadline?", "until when?"])

        _run_conversation(generator, FakeAgent(observed), SAYS_OK)

        assert generator.asked == [("and the deadline?", 2, ("I want to cancel my training",))]

    def test_hb16_a_failing_paraphrase_says_which_one_and_which_turn(self) -> None:
        # Two turns per phrasing: only the reply to "until when?" misses.
        agent = Replying(["ok", "ok", "ok", "ok", "ok", "no idea"])

        summary = _run_conversation(
            _Generator(["what about the deadline?", "until when?"]), agent, SAYS_OK
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert any(e.startswith('[variation: "until when?"] turn 2/2') for e in test.errors), (
            test.errors
        )
