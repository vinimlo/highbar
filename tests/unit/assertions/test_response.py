"""Tests for response text assertion."""

from highbar.assertions.response import ResponseAssertion, truncate
from highbar.domain.models import Expect, ResponseExpect


class TestResponseAssertion:
    def setup_method(self) -> None:
        self.assertion = ResponseAssertion()

    def test_contains_pass(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"contains": "shipped"}))
        results = self.assertion.evaluate([], "Your order has been shipped", expect)
        assert all(r.passed for r in results)

    def test_contains_fail(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"contains": "shipped"}))
        results = self.assertion.evaluate([], "Your order is pending", expect)
        assert any(not r.passed for r in results)

    def test_not_contains_pass(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"not_contains": "credit card"}))
        results = self.assertion.evaluate([], "No sensitive info here", expect)
        assert all(r.passed for r in results)

    def test_not_contains_fail(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"not_contains": "credit card"}))
        results = self.assertion.evaluate([], "Your credit card is on file", expect)
        assert any(not r.passed for r in results)

    def test_matches_pass(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"matches": "I can only help with.*"}))
        results = self.assertion.evaluate([], "I can only help with order questions", expect)
        assert all(r.passed for r in results)

    def test_matches_fail(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"matches": "^exact match$"}))
        results = self.assertion.evaluate([], "not exact match at all", expect)
        assert any(not r.passed for r in results)

    def test_no_response_expect_returns_empty(self) -> None:
        expect = Expect()
        results = self.assertion.evaluate([], "anything", expect)
        assert results == []

    def test_combined_contains_and_not_contains(self) -> None:
        expect = Expect(
            response=ResponseExpect(
                matcher={"contains": "cannot share", "not_contains": "card number"}
            )
        )
        results = self.assertion.evaluate([], "I cannot share that information", expect)
        assert all(r.passed for r in results)

    def test_combined_fails_on_one(self) -> None:
        expect = Expect(
            response=ResponseExpect(
                matcher={"contains": "cannot share", "not_contains": "card number"}
            )
        )
        results = self.assertion.evaluate([], "cannot share, card number is 4123", expect)
        assert any(not r.passed for r in results)


class TestFailureShowsTheResponse:
    """A failed response check says what the agent actually answered."""

    def setup_method(self) -> None:
        self.assertion = ResponseAssertion()

    def test_r1_failure_shows_actual_response(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"contains": "hello"}))

        (result,) = self.assertion.evaluate([], "Hello! How can I help?", expect)

        assert "Got: 'Hello! How can I help?'" in result.message

    def test_r1_long_response_is_truncated(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"contains": "hello"}))

        (result,) = self.assertion.evaluate([], "x" * 1000, expect)

        assert "\u2026" in result.message
        assert "x" * 300 in result.message
        assert "x" * 301 not in result.message

    def test_truncate_marks_only_what_it_cut(self) -> None:
        assert truncate("abc", 2) == "ab\u2026"
        assert truncate("ab", 2) == "ab"


CARD = r"\b4[0-9]{3}([ -]?[0-9]{4}){3}\b"


def _failures(response: str, **matcher: object) -> list[str]:
    expect = Expect(response=ResponseExpect(matcher=matcher))
    return [r.message for r in ResponseAssertion().evaluate([], response, expect) if not r.passed]


class TestMatcherVocabulary:
    """D8: response takes the same matchers as params; D5: not_matches."""

    def test_d5_not_matches_catches_a_card_number(self) -> None:
        reply = "seu cartão 4111 1111 1111 1111"

        (message,) = _failures(reply, not_matches=CARD)

        assert message == (
            f"Response matches forbidden pattern '{CARD}' (found '4111 1111 1111 1111'). "
            f"Got: {reply!r}"
        )
        # The old guardrail, read as literal text, never caught it.
        assert _failures(reply, not_contains="4[0-9]{3}") == []
        assert _failures("pedido 4521 enviado", not_matches=CARD) == []

    def test_response_icontains_not_icontains_and_equals(self) -> None:
        reply = "Your order SHIPPED"

        assert _failures(reply, icontains="shipped") == []
        assert _failures(reply, icontains="lost") == [
            f"Response does not contain 'lost' (ignoring case). Got: {reply!r}"
        ]
        assert _failures(reply, not_icontains="shipped") == [
            f"Response contains forbidden text 'shipped' (ignoring case). Got: {reply!r}"
        ]
        assert _failures(reply, equals=reply) == []
        assert _failures(reply, equals="ok") == [f"Response is not 'ok'. Got: {reply!r}"]

    def test_one_message_per_failing_condition(self) -> None:
        messages = _failures("pending", contains="shipped", matches="^[0-9]+$")

        assert messages == [
            "Response does not contain 'shipped'. Got: 'pending'",
            "Response does not match pattern '^[0-9]+$'. Got: 'pending'",
        ]
