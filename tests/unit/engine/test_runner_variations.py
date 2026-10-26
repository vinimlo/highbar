"""Tests for running a trigger together with its generated variations."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from rich.console import Console

from highbar.domain.models import (
    AssertionResult,
    Expect,
    FunctionCallExpect,
    Hello,
    Spec,
    Status,
    Suite,
    Test,
    Trace,
    Window,
)
from highbar.engine.runner import TestRunner
from highbar.reporters.terminal import TerminalReporter
from highbar.variations.generator import VariationError


class _Collector:
    def __init__(self, by_trigger: dict[str, list[str]]) -> None:
        self._by_trigger = by_trigger
        self.last_trigger = ""

    def checkpoint(self) -> int:
        return 0

    def collect(self, since: int) -> Window:
        tools = self._by_trigger.get(self.last_trigger, [])
        return Window(
            tools=[Trace(fn=fn, args={}, result={}, started_at=1.0, ended_at=1.1) for fn in tools],
            hellos=[Hello(ts=0.0)],
        )

    def declared(self) -> frozenset[str] | None:
        return None  # an agent that declares nothing, as on the rc1 SDK


class _Client:
    def __init__(self, collector: _Collector) -> None:
        self._collector = collector
        self.sent: list[str] = []

    def send(self, trigger: str, *, values: object) -> str:
        self.sent.append(trigger)
        self._collector.last_trigger = trigger
        return "ok"


class _Assertion:
    """Passes when the expected tool is among the traces."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[Any]:
        if not expect.function_calls:
            return []
        expected = expect.function_calls[0].name
        called = [t.fn for t in traces]
        passed = expected in called
        return [
            AssertionResult(
                passed=passed,
                message="" if passed else f"Expected '{expected}', got {called}",
            )
        ]


def _spec(variations: int) -> Spec:
    test = Test(
        trigger="show me the menu",
        expect=Expect(function_calls=[FunctionCallExpect(name="list_categories")]),
        variations=variations,
    )
    return Spec(agent=Mock(), suites=[Suite(name="Menu", tests=[test])])


def _runner(collector: _Collector, client: _Client, generator: Any) -> TestRunner:
    return TestRunner(
        adapter=Mock(),
        client=client,  # type: ignore[arg-type]
        collector=collector,  # type: ignore[arg-type]
        assertions=[_Assertion()],
        reporter=Mock(),
        settle_time=0.0,
        variations=generator,
    )


class _Generator:
    def __init__(self, variations: list[str]) -> None:
        self._variations = variations

    def generate(self, trigger: str, count: int) -> list[str]:
        return self._variations


class TestVariationExpansion:
    def test_original_trigger_and_each_variation_are_sent(self) -> None:
        collector = _Collector({})
        client = _Client(collector)
        generator = _Generator(["gimme the menu", "what do you serve"])

        _runner(collector, client, generator).run(_spec(variations=2))

        assert client.sent == ["show me the menu", "gimme the menu", "what do you serve"]

    def test_passes_when_every_phrasing_routes_correctly(self) -> None:
        collector = _Collector(
            {
                "show me the menu": ["list_categories"],
                "gimme the menu": ["list_categories"],
            }
        )
        client = _Client(collector)

        summary = _runner(collector, client, _Generator(["gimme the menu"])).run(_spec(1))

        assert summary.passed == 1
        assert summary.failed == 0

    def test_one_bad_phrasing_fails_the_test(self) -> None:
        """An agent that handles 9 wordings and drops the 10th is broken for that user."""
        collector = _Collector(
            {
                "show me the menu": ["list_categories"],
                "gimme the menu": ["confirm_order"],  # routed wrong
            }
        )
        client = _Client(collector)

        summary = _runner(collector, client, _Generator(["gimme the menu"])).run(_spec(1))

        assert summary.failed == 1

    def test_every_phrasing_runs_even_after_one_fails(self) -> None:
        """3-of-10 broken is a different bug from 1-of-10; stopping early hides which."""
        collector = _Collector({"show me the menu": ["list_categories"]})
        client = _Client(collector)

        _runner(collector, client, _Generator(["a", "b", "c"])).run(_spec(3))

        assert client.sent == ["show me the menu", "a", "b", "c"]

    def test_failure_names_the_phrasing_that_broke(self) -> None:
        collector = _Collector({"show me the menu": ["list_categories"]})
        client = _Client(collector)
        reporter = Mock()

        runner = TestRunner(
            adapter=Mock(),
            client=client,  # type: ignore[arg-type]
            collector=collector,  # type: ignore[arg-type]
            assertions=[_Assertion()],
            reporter=reporter,
            settle_time=0.0,
            variations=_Generator(["gimme the menu"]),
        )
        runner.run(_spec(1))

        _, result = reporter.on_test_result.call_args[0]
        assert any('[variation: "gimme the menu"]' in error for error in result.errors)

    def test_variations_are_counted_as_executions_not_tests(self) -> None:
        collector = _Collector({"show me the menu": ["list_categories"]})
        client = _Client(collector)

        summary = _runner(collector, client, _Generator(["a", "b"])).run(_spec(2))

        assert summary.total == 1
        assert summary.variations == 2

    def test_asking_for_variations_without_a_generator_is_an_error(self) -> None:
        """Better to stop than to quietly test one phrasing when the spec asked for five."""
        collector = _Collector({})
        client = _Client(collector)

        runner = TestRunner(
            adapter=Mock(),
            client=client,  # type: ignore[arg-type]
            collector=collector,  # type: ignore[arg-type]
            assertions=[_Assertion()],
            reporter=Mock(),
            settle_time=0.0,
        )

        with pytest.raises(ValueError, match="no variation generator"):
            runner.run(_spec(5))

    def test_original_trigger_is_not_labelled_a_variation(self) -> None:
        collector = _Collector({"gimme the menu": ["list_categories"]})
        client = _Client(collector)
        reporter = Mock()

        runner = _runner(collector, client, _Generator(["gimme the menu"]))
        runner._reporter = reporter
        runner.run(_spec(1))

        _, result = reporter.on_test_result.call_args[0]
        assert len(result.errors) == 1
        assert result.errors[0].startswith('[trigger: "show me the menu"]')

    def test_title_reports_how_many_variations_actually_ran(self) -> None:
        """The spec asked for 5; if only 2 exist, "5 variations" overstates the coverage."""
        collector = _Collector({})
        client = _Client(collector)
        reporter = Mock()

        runner = _runner(collector, client, _Generator(["a", "b"]))
        runner._reporter = reporter
        runner.run(_spec(5))

        name, _ = reporter.on_test_result.call_args[0]
        assert name == "show me the menu (2 of 5 variations)"


class _FailingGenerator:
    def generate(self, trigger: str, count: int) -> list[str]:
        raise RuntimeError("No cached variations and HIGHBAR_LLM_API_KEY is not set")


class TestGeneratorFailure:
    def _spec_with_two_tests(self) -> Spec:
        expect = Expect(function_calls=[FunctionCallExpect(name="list_categories")])
        return Spec(
            agent=Mock(),
            suites=[
                Suite(
                    name="Menu",
                    tests=[
                        Test(trigger="show me the menu", expect=expect, variations=3),
                        Test(trigger="what's on the menu", expect=expect),
                    ],
                )
            ],
        )

    def test_it_errors_that_test_and_the_run_goes_on(self) -> None:
        """A missing cache is one test in error, not a traceback that loses every other result."""
        collector = _Collector({"what's on the menu": ["list_categories"]})
        client = _Client(collector)
        reporter = Mock()
        runner = _runner(collector, client, _FailingGenerator())
        runner._reporter = reporter

        summary = runner.run(self._spec_with_two_tests())

        assert (summary.total, summary.passed, summary.failed, summary.errored) == (2, 1, 0, 1)
        _, failed = reporter.on_test_result.call_args_list[0][0]
        assert "Could not get variations" in failed.errors[0]
        assert "HIGHBAR_LLM_API_KEY" in failed.errors[0]
        # The agent is not sent a phrasing for a test whose variations are unavailable.
        assert client.sent == ["what's on the menu"]

    def test_variation_generation_failure_is_error(self) -> None:
        """Without the paraphrases the test was not run, so it cannot have failed."""

        class _Refusing:
            def generate(self, trigger: str, count: int) -> list[str]:
                raise VariationError("No cached variations for 'show me the menu'")

        collector = _Collector({})
        client = _Client(collector)
        reporter = Mock()
        runner = _runner(collector, client, _Refusing())
        runner._reporter = reporter

        runner.run(_spec(3))

        _, result = reporter.on_test_result.call_args[0]
        assert result.status is Status.ERROR


class TestTerminalOutput:
    """With the real reporter: Rich must not eat the bracketed phrasing as a style tag."""

    def test_failing_phrasing_reaches_the_terminal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        collector = _Collector({"show me the menu": ["list_categories"]})
        client = _Client(collector)
        reporter = TerminalReporter()
        reporter._console = Console(width=200, force_terminal=False)
        runner = _runner(collector, client, _Generator(["gimme the [bold]menu"]))
        runner._reporter = reporter

        runner.run(_spec(1))

        output = capsys.readouterr().out
        assert '[variation: "gimme the [bold]menu"]' in output
