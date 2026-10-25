"""Tests for domain model construction and defaults."""

from highbar.domain.models import (
    AgentConfig,
    AssertionResult,
    Expect,
    FunctionCallExpect,
    RunSummary,
    Test,
    TestResult,
    Trace,
    Turn,
)


class TestTrace:
    def test_create_trace(self) -> None:
        trace = Trace(
            id="abc123",
            seq=1,
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
            id="abc123",
            seq=1,
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
        result = TestResult(passed=True, errors=[], duration_ms=100.0)
        assert result.passed

    def test_failed(self) -> None:
        result = TestResult(
            passed=False,
            errors=["expected search_orders, got none"],
            duration_ms=200.0,
        )
        assert not result.passed
        assert len(result.errors) == 1


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
            passed=8,
            failed=2,
            duration_ms=5000.0,
        )
        assert summary.total == 10
        assert summary.failed == 2
