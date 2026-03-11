"""Tests for result assertion — validates tool return values."""

from gauntlet.assertions.result import ResultAssertion
from gauntlet.domain.models import Expect, FunctionCallExpect, Trace


def _make_trace(fn: str, result: object = None) -> Trace:
    return Trace(id="t1", seq=0, fn=fn, args={}, result=result, started_at=1.0, ended_at=1.1)


class TestResultAssertion:
    def setup_method(self) -> None:
        self.assertion = ResultAssertion()

    def test_result_match(self) -> None:
        traces = [_make_trace("search", result={"status": "shipped", "id": "123"})]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search", result={"status": "shipped"}),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_result_mismatch(self) -> None:
        traces = [_make_trace("search", result={"status": "pending"})]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search", result={"status": "shipped"}),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)

    def test_no_result_expected_skips(self) -> None:
        traces = [_make_trace("search", result={"anything": True})]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search"),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert all(r.passed for r in results)

    def test_no_function_calls_skips(self) -> None:
        expect = Expect()
        results = self.assertion.evaluate([], "", expect)
        assert all(r.passed for r in results)

    def test_result_on_non_dict_fails(self) -> None:
        traces = [_make_trace("search", result="just a string")]
        expect = Expect(
            function_calls=[
                FunctionCallExpect(name="search", result={"status": "shipped"}),
            ]
        )
        results = self.assertion.evaluate(traces, "", expect)
        assert any(not r.passed for r in results)
