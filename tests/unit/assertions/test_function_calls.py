"""Tests for function_calls assertion."""

from gauntlet.assertions.function_calls import FunctionCallsAssertion
from gauntlet.domain.models import Expect, FunctionCallExpect, Trace


def _make_trace(fn: str, args: dict | None = None, result: object = None, seq: int = 0) -> Trace:
    return Trace(
        id=f"trace_{seq}",
        seq=seq,
        fn=fn,
        args=args or {},
        result=result,
        started_at=1.0,
        ended_at=1.1,
    )


class TestFunctionCallsAssertion:
    def setup_method(self) -> None:
        self.assertion = FunctionCallsAssertion()

    def test_matching_call(self) -> None:
        traces = [_make_trace("search_orders", {"order_id": "123"})]
        expect = Expect(
            function_calls=[FunctionCallExpect(name="search_orders", params={"order_id": "123"})]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_missing_call(self) -> None:
        traces: list[Trace] = []
        expect = Expect(function_calls=[FunctionCallExpect(name="search_orders")])
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)
        assert "search_orders" in results[0].message

    def test_wrong_params(self) -> None:
        traces = [_make_trace("search_orders", {"order_id": "999"})]
        expect = Expect(
            function_calls=[FunctionCallExpect(name="search_orders", params={"order_id": "123"})]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)

    def test_subset_params_match(self) -> None:
        traces = [_make_trace("search", {"q": "test", "limit": 10, "offset": 0})]
        expect = Expect(function_calls=[FunctionCallExpect(name="search", params={"q": "test"})])
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_guardrail_empty_function_calls(self) -> None:
        traces = [_make_trace("search_orders")]
        expect = Expect(function_calls=[])
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)
        assert "no function calls" in results[0].message.lower()

    def test_guardrail_passes_when_no_calls(self) -> None:
        traces: list[Trace] = []
        expect = Expect(function_calls=[])
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_no_function_calls_in_expect_skips(self) -> None:
        traces = [_make_trace("whatever")]
        expect = Expect()
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_multiple_expected_calls(self) -> None:
        traces = [
            _make_trace("search_orders", {"order_id": "123"}, seq=0),
            _make_trace("get_details", {"order_id": "123"}, seq=1),
        ]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search_orders"),
                FunctionCallExpect(name="get_details"),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_result_match(self) -> None:
        traces = [_make_trace("search_orders", result={"status": "shipped", "id": "123"})]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search_orders", result={"status": "shipped"}),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_result_mismatch(self) -> None:
        traces = [_make_trace("search_orders", result={"status": "pending"})]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search_orders", result={"status": "shipped"}),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)
