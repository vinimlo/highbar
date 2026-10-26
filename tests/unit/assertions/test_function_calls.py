"""Tests for function_calls assertion."""

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.domain.models import Expect, FunctionCallExpect, Trace


def _make_trace(fn: str, args: dict | None = None, result: object = None, seq: int = 0) -> Trace:
    return Trace(
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

    def test_result_on_non_dict_fails(self) -> None:
        traces = [_make_trace("search_orders", result="just a string")]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search_orders", result={"status": "shipped"}),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)

    def test_no_function_calls_returns_empty(self) -> None:
        """When expect.function_calls is None, return empty list (not applicable)."""
        traces = [_make_trace("whatever")]
        expect = Expect()
        results = self.assertion.evaluate(traces, "", expect)
        assert results == []


class TestFunctionCallAlternatives:
    """`name` accepting a list: any of the listed tools satisfies the expectation."""

    def setup_method(self) -> None:
        self.assertion = FunctionCallsAssertion()

    def test_any_listed_tool_satisfies(self) -> None:
        expect = Expect(
            function_calls=[FunctionCallExpect(name=["open_catalog", "list_categories"])]
        )

        for called in ("open_catalog", "list_categories"):
            results = self.assertion.evaluate([_make_trace(called)], "", expect)
            assert all(r.passed for r in results), called

    def test_tool_outside_the_list_fails(self) -> None:
        expect = Expect(
            function_calls=[FunctionCallExpect(name=["open_catalog", "list_categories"])]
        )

        results = self.assertion.evaluate([_make_trace("confirm_order")], "", expect)

        assert not results[0].passed

    def test_failure_message_lists_alternatives_and_what_was_called(self) -> None:
        expect = Expect(
            function_calls=[FunctionCallExpect(name=["open_catalog", "list_categories"])]
        )

        results = self.assertion.evaluate([_make_trace("confirm_order")], "", expect)

        assert "open_catalog" in results[0].message
        assert "list_categories" in results[0].message
        assert "confirm_order" in results[0].message

    def test_params_are_matched_against_the_alternative_that_fits(self) -> None:
        """With several candidates, one matching params/result is enough."""
        expect = Expect(
            function_calls=[
                FunctionCallExpect(
                    name=["search_products", "list_products"], params={"query": "pizza"}
                )
            ]
        )
        traces = [
            _make_trace("search_products", {"query": "coke"}, seq=0),
            _make_trace("list_products", {"query": "pizza"}, seq=1),
        ]

        results = self.assertion.evaluate(traces, "", expect)

        assert all(r.passed for r in results)

    def test_right_tool_wrong_params_reports_the_tool_that_was_called(self) -> None:
        expect = Expect(
            function_calls=[
                FunctionCallExpect(
                    name=["search_products", "list_products"], params={"query": "pizza"}
                )
            ]
        )

        results = self.assertion.evaluate(
            [_make_trace("list_products", {"query": "coke"})], "", expect
        )

        assert not results[0].passed
        assert "'list_products' was called but didn't match" in results[0].message
