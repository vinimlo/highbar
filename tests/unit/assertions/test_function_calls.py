"""Tests for function_calls assertion."""

from highbar.assertions.function_calls import FunctionCallsAssertion, satisfies
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


def _call(fn: str, error: str | None = None, **args: object) -> Trace:
    return Trace(fn=fn, args=dict(args), result=None, started_at=1.0, ended_at=1.1, error=error)


def _failures(traces: list[Trace], expect: Expect) -> list[str]:
    return [
        r.message for r in FunctionCallsAssertion().evaluate(traces, "", expect) if not r.passed
    ]


class TestDistinctCallsAndFailures:
    """A2, A3, A7: each expectation needs its own call, and calling is not working."""

    def test_a7_failed_call_does_not_satisfy_a_positive_expectation(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="get_order")])

        (message,) = _failures([_call("get_order", error="Timeout")], expect)

        assert message == "'get_order' was called but failed: Timeout"

    def test_a7_expected_failure_with_error_true_and_with_a_matcher(self) -> None:
        failed = [_call("get_order", error="TimeoutError: upstream timed out")]
        worked = [_call("get_order")]

        anything = Expect(function_calls=[FunctionCallExpect(name="get_order", error=True)])
        timeout = Expect(
            function_calls=[FunctionCallExpect(name="get_order", error={"contains": "Timeout"})]
        )
        other = Expect(
            function_calls=[FunctionCallExpect(name="get_order", error={"contains": "404"})]
        )

        assert _failures(failed, anything) == []
        assert _failures(failed, timeout) == []
        assert _failures(failed, other) != []
        assert _failures(worked, anything) != []

    def test_a7_same_tool_listed_twice_needs_two_calls(self) -> None:
        expect = Expect(
            function_calls=[FunctionCallExpect(name="search"), FunctionCallExpect(name="search")]
        )

        (message,) = _failures([_call("search", q="a")], expect)

        assert message == "expected 2 calls to 'search', got 1"
        assert _failures([_call("search", q="a"), _call("search", q="b")], expect) == []

    def test_distinct_matching_finds_the_assignment_greedy_would_miss(self) -> None:
        """Greedy gives search(q="a") to the bare expectation and has nothing left."""
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search"),
                FunctionCallExpect(name="search", params={"q": "a"}),
            ]
        )

        assert _failures([_call("search", q="a"), _call("search", q="b")], expect) == []

    def test_a2_ordered_fails_when_calls_are_out_of_order(self) -> None:
        calls = [_call("b"), _call("a")]
        expected = [FunctionCallExpect(name="a"), FunctionCallExpect(name="b")]

        (message,) = _failures(calls, Expect(function_calls=expected, ordered=True))

        assert message == "calls matched but out of order: expected a, b; got b, a"
        assert _failures(calls, Expect(function_calls=expected)) == []

    def test_ordered_passes_with_unrelated_calls_in_between(self) -> None:
        calls = [_call("a"), _call("log"), _call("b")]
        expected = [FunctionCallExpect(name="a"), FunctionCallExpect(name="b")]

        assert _failures(calls, Expect(function_calls=expected, ordered=True)) == []

    def test_a3_exclusive_fails_on_an_extra_call(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="get_order")], exclusive=True)

        (message,) = _failures([_call("get_order"), _call("delete_account")], expect)

        assert message == "unexpected calls: delete_account"
        assert _failures([_call("get_order")], expect) == []

    def test_forbid_fails_even_when_the_forbidden_call_errored(self) -> None:
        expect = Expect(forbid=("delete_account",))

        (message,) = _failures([_call("delete_account", error="PermissionError")], expect)

        assert message == "forbidden tool 'delete_account' was called"
        assert _failures([_call("get_order")], expect) == []

    def test_forbid_alongside_expected_calls(self) -> None:
        expect = Expect(
            function_calls=[FunctionCallExpect(name="get_order")], forbid=("delete_account",)
        )

        assert _failures([_call("get_order"), _call("delete_account")], expect) == [
            "forbidden tool 'delete_account' was called"
        ]

    def test_satisfies_checks_name_payload_and_error(self) -> None:
        expected = FunctionCallExpect(name=["get_order", "find_order"], params={"id": 7})

        assert satisfies(expected, _call("find_order", id=7))
        assert not satisfies(expected, _call("find_order", id=8))
        assert not satisfies(expected, _call("find_order", error="boom", id=7))
        assert not satisfies(expected, _call("cancel_order", id=7))
