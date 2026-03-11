"""Tests for response text assertion."""

from gauntlet.assertions.response import ResponseAssertion
from gauntlet.domain.models import Expect, ResponseExpect


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
        expect = Expect(response=ResponseExpect(not_contains="4[0-9]{3}"))
        results = self.assertion.evaluate([], "No credit card info here", expect)
        assert all(r.passed for r in results)

    def test_not_contains_fail_regex(self) -> None:
        expect = Expect(response=ResponseExpect(not_contains="4[0-9]{3}"))
        results = self.assertion.evaluate([], "Card: 4123-5678", expect)
        assert any(not r.passed for r in results)

    def test_matches_pass(self) -> None:
        expect = Expect(response=ResponseExpect(matches="I can only help with.*"))
        results = self.assertion.evaluate([], "I can only help with order questions", expect)
        assert all(r.passed for r in results)

    def test_matches_fail(self) -> None:
        expect = Expect(response=ResponseExpect(matches="^exact match$"))
        results = self.assertion.evaluate([], "not exact match at all", expect)
        assert any(not r.passed for r in results)

    def test_no_response_expect_skips(self) -> None:
        expect = Expect()
        results = self.assertion.evaluate([], "anything", expect)
        assert all(r.passed for r in results)

    def test_combined_contains_and_not_contains(self) -> None:
        expect = Expect(
            response=ResponseExpect(
                contains="cannot share",
                not_contains="4[0-9]{3}",
            )
        )
        results = self.assertion.evaluate([], "I cannot share that information", expect)
        assert all(r.passed for r in results)

    def test_combined_fails_on_one(self) -> None:
        expect = Expect(
            response=ResponseExpect(
                contains="cannot share",
                not_contains="4[0-9]{3}",
            )
        )
        results = self.assertion.evaluate([], "cannot share, card is 4123", expect)
        assert any(not r.passed for r in results)
