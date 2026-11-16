"""HB-30: with agent.llm_proxy, what the proxy read is the usage budgets and --max-cost hold.

The fake agent writes, for each message, its own `turn` and the `llm` records the proxy
would write for the model calls it made. Choices P3 to P8 are on the card.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from typing import Any
from unittest.mock import MagicMock

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Budget,
    Expect,
    LlmCall,
    LlmExpect,
    ResponseExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    TurnRecord,
    Usage,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.runner import TestRunner as Runner
from tests.unit.engine.fakes import FakeAgent

SAYS_OK = ResponseExpect(matcher={"contains": "ok"})
UNDER_500 = Budget(max_input_tokens=500)


class _Proxy:
    """What the runner reads of the proxy: how many records the trace file refused."""

    def __init__(self, write_errors: int = 0) -> None:
        self.write_errors = write_errors


def _model_call(
    input_tokens: int | None = 1000,
    output_tokens: int | None = 20,
    *,
    model: str = "gpt-4o-mini",
    provider: str | None = "openai",
    read: bool = True,
    unread: str | None = None,
    cost: float | None = None,
    test_id: str | None = None,
) -> LlmCall:
    return LlmCall(
        ts=1.0,
        context={"test": test_id} if test_id else {},
        model=model,
        provider=provider,
        input_tokens=input_tokens if read else None,
        output_tokens=output_tokens if read else None,
        source="proxy",
        read=read,
        unread=unread,
        call="model",
        cost=cost,
        currency="USD" if cost is not None else None,
    )


def _passthrough(reason: str = "the proxy does not read GET /models of the 'openai' upstream"):
    return LlmCall(ts=1.0, source="proxy", read=False, unread=reason, call="other")


Writes = Callable[[str], tuple[list[LlmCall], Usage | None]]


def _agent(
    *messages: tuple[Sequence[LlmCall], Usage | None],
    late: Sequence[LlmCall] = (),
    agent_llms: Sequence[LlmCall] = (),
    tagged: Sequence[LlmCall] = (),
) -> FakeAgent:
    """Each message: the proxy's records of its calls, and the usage its own `turn` says.

    ``late``: proxy records of the first message, written after its window, tagged.
    ``agent_llms``: the agent's own `llm` records; ``tagged``: proxy records that carry
    the test id, as when the agent's client forwards X-Highbar-Test.
    """
    sent = itertools.count()

    def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        number = next(sent)
        calls, usage = messages[min(number, len(messages) - 1)]
        turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=usage)
        own = [
            LlmCall(ts=1.0, context={"test": test_id}, model=c.model, input_tokens=c.input_tokens)
            for c in agent_llms
        ]
        with_id = [LlmCall(**{**c.__dict__, "context": {"test": test_id}}) for c in tagged]
        now = Window(turns=[turn], llms=[*calls, *own, *with_id])
        if number == 0 and late:
            later = [LlmCall(**{**c.__dict__, "context": {"test": test_id}}) for c in late]
            return now, Window(llms=later)
        return now

    return FakeAgent(script)


def _run(
    tests: list[Case],
    agent: FakeAgent,
    *,
    proxy: _Proxy | None,
    max_cost: float | None = None,
    reporter: MagicMock | None = None,
    **options: Any,
) -> RunSummary:
    extra: dict[str, Any] = {} if proxy is None else {"proxy": proxy}
    runner = Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        managed=True,
        max_cost=max_cost,
        **extra,
        **options,
    )
    spec = Spec(
        agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
        suites=[Suite(name="S", tests=tests)],
    )
    return runner.run(spec)


def _budgeted(budget: Budget = UNDER_500, name: str = "orders") -> Case:
    return Case(name=name, trigger="t", expect=Expect(response=SAYS_OK, budget=budget))


def _warnings(reporter: MagicMock) -> list[str]:
    return [call.args[0] for call in reporter.on_warning.call_args_list]


TEN = Usage(model="gpt-4o-mini", input_tokens=10, output_tokens=1)


class TestTheProxysCountHolds:
    def test_hb30_the_proxys_tokens_replace_the_turns_and_fail_the_budget(self) -> None:
        # The gap on the card: the turn says 10 tokens, the model was sent 1000.
        agent = _agent(([_model_call(1000)], TEN))

        summary = _run([_budgeted()], agent, proxy=_Proxy())

        (test,) = summary.tests
        assert test.status is Status.FAILED, test.errors
        assert "used 1000 input tokens, over max_input_tokens 500" in "; ".join(test.errors)

    def test_hb30_without_the_proxy_the_turns_count_holds_as_before(self) -> None:
        agent = _agent(([_model_call(1000)], TEN))

        summary = _run([_budgeted()], agent, proxy=None)

        assert summary.tests[0].status is Status.PASSED, summary.tests[0].errors

    def test_hb30_the_turns_usage_is_shown_not_added(self) -> None:
        agent = _agent(([_model_call(300), _model_call(100)], TEN))

        summary = _run([_budgeted()], agent, proxy=_Proxy())

        (test,) = summary.tests
        assert test.status is Status.PASSED, test.errors
        (turn,) = test.phrasings[0].executions[0].turns
        assert turn.usage is not None and turn.usage.input_tokens == 400
        assert turn.usage.llm_calls == 2
        assert turn.agent_usage is not None and turn.agent_usage.input_tokens == 10

    def test_hb30_a_turn_with_no_proxy_record_keeps_its_own_usage(self) -> None:
        # Without proxy records, nothing changes (plan, point 6).
        agent = _agent(([], Usage(model="gpt-4o-mini", input_tokens=900, output_tokens=1)))

        summary = _run([_budgeted()], agent, proxy=_Proxy())

        assert summary.tests[0].status is Status.FAILED, summary.tests[0].errors

    def test_hb30_a_late_proxy_record_counts_for_its_test(self) -> None:
        agent = _agent(([_model_call(100)], TEN), late=[_model_call(900)])

        summary = _run([_budgeted(), _budgeted(name="next")], agent, proxy=_Proxy())

        first = summary.tests[0]
        assert first.status is Status.FAILED, first.errors
        assert "after the test's window closed" in "; ".join(first.errors)


class TestUnread:
    def test_hb30_an_unread_model_call_leaves_the_budget_error_never_zero(self) -> None:
        unread = _model_call(read=False, unread="the stream ended without usage")
        agent = _agent(([unread], TEN))

        summary = _run([_budgeted()], agent, proxy=_Proxy())

        (test,) = summary.tests
        assert test.status is Status.ERROR, test.errors
        assert "the stream ended without usage" in "; ".join(test.errors)

    def test_hb30_an_unread_model_call_with_max_cost_is_an_error(self) -> None:
        unread = _model_call(read=False, unread="the stream ended without usage")
        plain = Case(name="orders", trigger="t", expect=Expect(response=SAYS_OK))

        summary = _run([plain], _agent(([unread], TEN)), proxy=_Proxy(), max_cost=1.0)

        (test,) = summary.tests
        assert test.status is Status.ERROR, test.errors
        assert "--max-cost" in "; ".join(test.errors)

    def test_hb30_a_call_that_is_not_a_model_call_is_not_read_and_not_an_error(self) -> None:
        reporter = MagicMock()
        agent = _agent(([_passthrough(), _model_call(100)], TEN))

        summary = _run([_budgeted()], agent, proxy=_Proxy(), reporter=reporter)

        assert summary.tests[0].status is Status.PASSED, summary.tests[0].errors
        assert any("not read" in w and "GET /models" in w for w in _warnings(reporter))


class TestBypass:
    def test_hb30_a_turn_over_what_the_proxy_saw_warns_and_the_larger_count_holds(self) -> None:
        reporter = MagicMock()
        agent = _agent(([_model_call(10)], Usage(model="gpt-4o-mini", input_tokens=1000)))

        summary = _run([_budgeted()], agent, proxy=_Proxy(), reporter=reporter)

        (test,) = summary.tests
        assert test.status is Status.FAILED, test.errors
        assert "used 1000 input tokens" in "; ".join(test.errors)
        (warning,) = [w for w in _warnings(reporter) if "went around" in w]
        assert "'orders'" in warning


class TestCost:
    def test_hb30_the_proxys_cost_outranks_the_agents_cost_zero(self) -> None:
        # A BYOK key reports cost 0: the proxy read 1000 priced tokens, a dated name.
        byok = Usage(model="gpt-4.1", input_tokens=1000, output_tokens=1000, cost=0.0)
        byok = Usage(**{**byok.__dict__, "currency": "USD"})
        call = _model_call(1000, 1000, model="gpt-4.1-2025-04-14")
        plain = Case(name="orders", trigger="t", expect=Expect(response=SAYS_OK))

        summary = _run(
            [plain, Case(name="next", trigger="t", expect=Expect(response=SAYS_OK))],
            _agent(([call], byok)),
            proxy=_Proxy(),
            max_cost=0.001,
        )

        assert summary.stopped is not None and "--max-cost" in summary.stopped
        assert summary.total == 1  # it stopped after the first test
        cost = summary.tests[0].phrasings[0].executions[0].turns[0].usage
        assert cost is not None and cost.cost is not None and cost.cost > 0.001

    def test_hb30_the_providers_cost_is_kept(self) -> None:
        call = _model_call(10, 10, model="openai/gpt-4o-mini", provider=None, cost=0.25)
        plain = Case(name="orders", trigger="t", expect=Expect(response=SAYS_OK))

        summary = _run([plain], _agent(([call], TEN)), proxy=_Proxy(), max_cost=0.1)

        assert summary.stopped is not None and "0.25 USD" in summary.stopped


class TestWriteErrors:
    def test_hb30_a_record_the_trace_refused_leaves_every_budget_error(self) -> None:
        agent = _agent(([_model_call(100)], TEN))

        summary = _run([_budgeted()], agent, proxy=_Proxy(write_errors=1))

        (test,) = summary.tests
        assert test.status is Status.ERROR, test.errors
        assert "could not write" in "; ".join(test.errors)

    def test_hb30_a_refused_record_later_in_the_run_errs_the_tests_before(self) -> None:
        proxy = _Proxy()
        sends = itertools.count()

        def script(message: str, test_id: str) -> Window:
            if next(sends) == 1:
                proxy.write_errors = 1  # during the second test
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=TEN)
            return Window(turns=[turn], llms=[_model_call(100)])

        clean = _run([_budgeted(), _budgeted(name="next")], FakeAgent(script), proxy=_Proxy())
        sends = itertools.count()
        summary = _run([_budgeted(), _budgeted(name="next")], FakeAgent(script), proxy=proxy)

        assert [t.status for t in clean.tests] == [Status.PASSED, Status.PASSED]
        assert [t.status for t in summary.tests] == [Status.ERROR, Status.ERROR]
        assert "could not write" in "; ".join(summary.tests[0].errors)


class TestExpectLlmReadsTheAgentsRecords:
    def test_hb30_max_calls_does_not_count_the_proxys_records(self) -> None:
        expect = Expect(response=SAYS_OK, llm=LlmExpect(max_calls=1))
        agent = _agent(([], TEN), agent_llms=[_model_call(10)], tagged=[_model_call(10)] * 2)

        summary = _run([Case(name="o", trigger="t", expect=expect)], agent, proxy=_Proxy())

        assert summary.tests[0].status is Status.PASSED, summary.tests[0].errors

    def test_hb30_proxy_records_alone_do_not_satisfy_expect_llm(self) -> None:
        expect = Expect(response=SAYS_OK, llm=LlmExpect(max_calls=5))
        agent = _agent(([], TEN), tagged=[_model_call(10)])

        summary = _run([Case(name="o", trigger="t", expect=expect)], agent, proxy=_Proxy())

        assert summary.tests[0].status is Status.ERROR, summary.tests[0].errors
