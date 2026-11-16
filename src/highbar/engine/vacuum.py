"""Tests that an agent doing nothing useful would pass (HB-47).

highbar makes sure what it observed is honest; this makes sure a test can fail. Each
test is judged, with the assertions a run uses, against what built-in trivial agents
would have done. A test one of them passes is vacuous: green on it says nothing about
the agent under test.

No agent runs. Each trivial agent's observation is built here, as if it had written
honest traces: a `turn` for each message, and its own calls. So the probe is exact and
free, and it needs neither the spec's agent nor its sessions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import Expect, Spec, Test, Trace, Turn

# Response matchers whose text operand a reply could hold word for word.
_POSITIVE_TEXT = ("contains", "icontains")


@dataclass(frozen=True)
class Finding:
    """One test: the trivial agents that pass it, and what could not be judged."""

    suite: str
    test: str
    passes_on: tuple[str, ...]
    not_judged: str | None = None


@dataclass(frozen=True)
class _Doing:
    """What a trivial agent does with one message: its reply, and the tools it calls."""

    reply: str
    calls: tuple[str, ...] = ()


TrivialAgent = Callable[[str], _Doing]


def probe_spec(spec: Spec) -> list[Finding]:
    """Every test but the negative controls (``expect_status``), in spec order."""
    tools = _tool_names(spec)
    every_text = _texts_looked_for(_expects(spec))
    findings: list[Finding] = []
    for suite in spec.suites:
        for test in suite.tests:
            if test.expect_status is not None:
                continue  # a test meant to fail is a control, not a claim
            not_judged = _beyond_trivial(test)
            agents = _agents(tools, every_text, test)
            passes_on = (
                ()
                if not_judged
                else tuple(name for name, agent in agents.items() if _passes(test, agent))
            )
            findings.append(Finding(suite.name, test.plain_name, passes_on, not_judged))
    return findings


def _agents(tools: tuple[str, ...], every_text: list[str], test: Test) -> dict[str, TrivialAgent]:
    agents: dict[str, TrivialAgent] = {
        "silent": lambda _message: _Doing(reply=""),
        "echo": lambda message: _Doing(reply=message),
        "every tool": lambda _message: _Doing(reply="", calls=tools),
    }
    # "every option" names each text the spec looks for, this test's among them. It
    # says something only when it also names one this test does not ask for: alone,
    # the test's own text is just the right answer (review of #76).
    own = set(_texts_looked_for(turn.expect for turn in _turns(test)))
    if any(text not in own for text in every_text):
        everything = " · ".join(every_text)
        agents["every option"] = lambda _message: _Doing(reply=everything)
    return agents


def _passes(test: Test, agent: TrivialAgent) -> bool:
    """Every turn of the test holds on what the agent did with its message."""
    return all(_holds(turn.expect, agent(turn.trigger)) for turn in _turns(test))


def _holds(expect: Expect, doing: _Doing) -> bool:
    calls = [
        Trace(fn=name, args={}, result=None, started_at=n, ended_at=n + 0.5)
        for n, name in enumerate(doing.calls)
    ]
    # A budget holds: a trivial agent spends nothing, which is under any limit.
    return all(
        result.passed
        for assertion in (FunctionCallsAssertion(), ResponseAssertion())
        for result in assertion.evaluate(calls, doing.reply, expect)
    )


def _beyond_trivial(test: Test) -> str | None:
    """What a trivial agent cannot satisfy, so the test is not judged vacuous.

    A trivial agent records no call to the model and nothing that entered a prompt, and
    the author's own check reads state no trivial agent changes: those parts fail on it.
    """
    for turn in _turns(test):
        if turn.expect.check is not None:
            return "expect.check"
        if turn.expect.llm is not None:
            return "expect.llm"
        if turn.expect.retrieval is not None:
            return "expect.retrieval"
    return None


def _turns(test: Test) -> list[Turn]:
    if test.turns:
        return list(test.turns)
    return [Turn(trigger=test.trigger or "", expect=test.expect or Expect())]


def _expects(spec: Spec) -> Iterator[Expect]:
    for suite in spec.suites:
        for test in suite.tests:
            for turn in _turns(test):
                yield turn.expect


def _tool_names(spec: Spec) -> tuple[str, ...]:
    """Each tool the spec names, expected or forbidden: what "every tool" calls."""
    names: dict[str, None] = {}
    for expect in _expects(spec):
        for call in expect.function_calls or []:
            names.update(dict.fromkeys(call.accepted_names))
        names.update(dict.fromkeys(expect.forbid))
    return tuple(names)


def _texts_looked_for(expects: Iterable[Expect]) -> list[str]:
    """Every text these replies must contain: what "every option" names at once."""
    texts: dict[str, None] = {}
    for expect in expects:
        if expect.response is None:
            continue
        for key in _POSITIVE_TEXT:
            operand = expect.response.matcher.get(key)
            if isinstance(operand, str):
                texts[operand] = None
    return list(texts)
