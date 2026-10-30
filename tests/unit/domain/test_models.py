"""Tests for domain model construction and defaults."""

from dataclasses import replace

from highbar.domain.models import (
    AgentConfig,
    AssertionResult,
    Expect,
    FunctionCallExpect,
    RunSummary,
    Status,
    Test,
    TestResult,
    Trace,
    Turn,
    combine,
    stable_key,
)


class TestTrace:
    def test_create_trace(self) -> None:
        trace = Trace(
            fn="search_orders",
            args={"order_id": "12345"},
            result={"status": "shipped"},
            started_at=1710000000.0,
            ended_at=1710000000.5,
        )
        assert trace.fn == "search_orders"
        assert trace.args == {"order_id": "12345"}
        assert trace.duration_ms == 500.0

    def test_trace_duration(self) -> None:
        trace = Trace(
            fn="fast_fn",
            args={},
            result=None,
            started_at=1.0,
            ended_at=1.05,
        )
        assert abs(trace.duration_ms - 50.0) < 0.01


class TestAgentConfig:
    def test_defaults(self) -> None:
        config = AgentConfig(
            start="python -m agent",
            endpoint="http://localhost:8000/chat",
        )
        assert config.startup_timeout == 15000
        assert config.settle_time == 500
        assert config.health is None

    def test_custom_values(self) -> None:
        config = AgentConfig(
            start="python -m agent",
            endpoint="http://localhost:8000/chat",
            health="http://localhost:8000/health",
            startup_timeout=30000,
            settle_time=1000,
        )
        assert config.health == "http://localhost:8000/health"
        assert config.startup_timeout == 30000


class TestFunctionCallExpect:
    def test_minimal(self) -> None:
        fc = FunctionCallExpect(name="search_orders")
        assert fc.params is None
        assert fc.result is None

    def test_full(self) -> None:
        fc = FunctionCallExpect(
            name="search_orders",
            params={"order_id": "12345"},
            result={"status": "shipped"},
        )
        assert fc.params == {"order_id": "12345"}


class TestExpect:
    def test_defaults(self) -> None:
        expect = Expect()
        assert expect.function_calls is None
        assert expect.response is None

    def test_empty_function_calls_guardrail(self) -> None:
        expect = Expect(function_calls=[])
        assert expect.function_calls == []


class TestTest:
    def test_single_turn(self) -> None:
        test = Test(trigger="hello", expect=Expect())
        assert test.trigger == "hello"
        assert test.turns is None

    def test_multi_turn(self) -> None:
        turns = [
            Turn(trigger="first", expect=Expect()),
            Turn(trigger="second", expect=Expect()),
        ]
        test = Test(turns=turns)
        assert test.trigger is None
        assert len(test.turns) == 2


class TestTestResult:
    def test_passed(self) -> None:
        result = TestResult(status=Status.PASSED, errors=[], duration_ms=100.0)
        assert result.passed

    def test_failed(self) -> None:
        result = TestResult(
            status=Status.FAILED,
            errors=["expected search_orders, got none"],
            duration_ms=200.0,
        )
        assert not result.passed
        assert len(result.errors) == 1

    def test_error_is_not_passed(self) -> None:
        result = TestResult(status=Status.ERROR, errors=["HTTP error: refused"], duration_ms=1.0)
        assert not result.passed


class TestCombine:
    def test_combine_prefers_failed_over_error(self) -> None:
        """A wrong answer is the stronger finding: it holds even if another part errored."""
        assert combine([Status.ERROR, Status.FAILED, Status.PASSED]) is Status.FAILED
        assert combine([Status.PASSED, Status.ERROR]) is Status.ERROR
        assert combine([]) is Status.PASSED


class TestAssertionResult:
    def test_pass(self) -> None:
        r = AssertionResult(passed=True, message="")
        assert r.passed

    def test_fail(self) -> None:
        r = AssertionResult(passed=False, message="expected X got Y")
        assert not r.passed


class TestRunSummary:
    def test_summary(self) -> None:
        summary = RunSummary(
            total=10,
            passed=7,
            failed=2,
            errored=1,
            duration_ms=5000.0,
        )
        assert summary.total == 10
        assert summary.failed == 2
        assert summary.errored == 1


class TestKeyAndDefinition:
    def test_key_is_the_id_when_there_is_one(self) -> None:
        assert (
            stable_key("Orders", Test(id="status", name="A", trigger="t", expect=Expect()))
            == "status"
        )

    def test_key_falls_back_to_suite_and_name(self) -> None:
        assert stable_key("Orders", Test(name="A", trigger="t", expect=Expect())) == "Orders/A"

    def test_key_of_an_unnamed_test_is_its_trigger(self) -> None:
        assert stable_key("Orders", Test(trigger="t", expect=Expect())) == "Orders/t"

    def test_key_of_an_unnamed_conversation_counts_its_turns(self) -> None:
        turns = [Turn(trigger="a", expect=Expect()), Turn(trigger="b", expect=Expect())]
        assert stable_key("Orders", Test(turns=turns)) == "Orders/multi-turn (2 turns)"

    def test_j12_definition_changes_with_what_is_measured(self) -> None:
        base = Test(trigger="t", expect=Expect(forbid=("a",)))

        assert base.definition_sha256 != replace(base, trigger="other").definition_sha256
        assert (
            base.definition_sha256 != replace(base, expect=Expect(forbid=("b",))).definition_sha256
        )
        assert base.definition_sha256 != replace(base, variations=2).definition_sha256

    def test_j12_definition_ignores_labels_and_sampling(self) -> None:
        base = Test(trigger="t", expect=Expect(forbid=("a",)))

        for other in (
            replace(base, name="renamed"),
            replace(base, id="an-id"),
            replace(base, runs=10),
            replace(base, min_pass=0.8),
        ):
            assert other.definition_sha256 == base.definition_sha256

    def test_j12_definition_of_a_conversation_ignores_turn_names(self) -> None:
        turn = Turn(trigger="a", expect=Expect())
        named = replace(turn, name="greeting")

        assert Test(turns=[turn]).definition_sha256 == Test(turns=[named]).definition_sha256
        changed = replace(turn, trigger="b")
        assert Test(turns=[turn]).definition_sha256 != Test(turns=[changed]).definition_sha256
