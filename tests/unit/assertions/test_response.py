"""Tests for response text assertion."""

from highbar.assertions.response import ResponseAssertion, truncate
from highbar.domain.models import Expect, ResponseExpect


class TestResponseAssertion:
    def setup_method(self) -> None:
        self.assertion = ResponseAssertion()

    def test_contains_pass(self) -> None:
        expect = Expect(response=ResponseExpect(contains="shipped"))
        results = self.assertion.evaluate([], "Your order has been shipped", expect)
        assert all(r.passed for r in results)

    def test_contains_fail(self) -> None:
        expect = Expect(response=ResponseExpect(contains="shipped"))
        results = self.assertion.evaluate([], "Your order is pending", expect)
        assert any(not r.passed for r in results)

    def test_not_contains_pass(self) -> None:
        expect = Expect(response=ResponseExpect(not_contains="credit card"))
        results = self.assertion.evaluate([], "No sensitive info here", expect)
        assert all(r.passed for r in results)

    def test_not_contains_fail(self) -> None:
        expect = Expect(response=ResponseExpect(not_contains="credit card"))
        results = self.assertion.evaluate([], "Your credit card is on file", expect)
        assert any(not r.passed for r in results)

    def test_matches_pass(self) -> None:
        expect = Expect(response=ResponseExpect(matches="I can only help with.*"))
        results = self.assertion.evaluate([], "I can only help with order questions", expect)
        assert all(r.passed for r in results)

    def test_matches_fail(self) -> None:
        expect = Expect(response=ResponseExpect(matches="^exact match$"))
        results = self.assertion.evaluate([], "not exact match at all", expect)
        assert any(not r.passed for r in results)

    def test_no_response_expect_returns_empty(self) -> None:
        expect = Expect()
        results = self.assertion.evaluate([], "anything", expect)
        assert results == []

    def test_combined_contains_and_not_contains(self) -> None:
        expect = Expect(
            response=ResponseExpect(
                contains="cannot share",
                not_contains="card number",
            )
        )
        results = self.assertion.evaluate([], "I cannot share that information", expect)
        assert all(r.passed for r in results)

    def test_combined_fails_on_one(self) -> None:
        expect = Expect(
            response=ResponseExpect(
                contains="cannot share",
                not_contains="card number",
            )
        )
        results = self.assertion.evaluate([], "cannot share, card number is 4123", expect)
        assert any(not r.passed for r in results)


class TestFailureShowsTheResponse:
    """A failed response check says what the agent actually answered."""

    def setup_method(self) -> None:
        self.assertion = ResponseAssertion()

    def test_r1_failure_shows_actual_response(self) -> None:
        expect = Expect(response=ResponseExpect(contains="hello"))

        (result,) = self.assertion.evaluate([], "Hello! How can I help?", expect)

        assert "Got: 'Hello! How can I help?'" in result.message

    def test_r1_long_response_is_truncated(self) -> None:
        expect = Expect(response=ResponseExpect(contains="hello"))

        (result,) = self.assertion.evaluate([], "x" * 1000, expect)

        assert "\u2026" in result.message
        assert "x" * 300 in result.message
        assert "x" * 301 not in result.message

    def test_truncate_marks_only_what_it_cut(self) -> None:
        assert truncate("abc", 2) == "ab\u2026"
        assert truncate("ab", 2) == "ab"
