"""Response text assertion — validates response content."""

from __future__ import annotations

import re

from highbar.domain.models import AssertionResult, Expect, Trace


def truncate(text: str, limit: int = 300) -> str:
    """Cut ``text`` at ``limit`` characters, ending with an ellipsis only when it cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\u2026"


class ResponseAssertion:
    """Evaluates response text against expected patterns."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        if expect.response is None:
            return []

        results: list[AssertionResult] = []
        resp = expect.response
        # What the agent said is the first thing anyone asks when this fails.
        got = repr(truncate(response))

        if resp.contains is not None:
            if resp.contains in response:
                results.append(AssertionResult(passed=True, message=""))
            else:
                results.append(
                    AssertionResult(
                        passed=False,
                        message=f"Response does not contain '{resp.contains}'. Got: {got}",
                    )
                )

        if resp.not_contains is not None:
            if resp.not_contains in response:
                results.append(
                    AssertionResult(
                        passed=False,
                        message=(
                            f"Response contains forbidden text '{resp.not_contains}'. Got: {got}"
                        ),
                    )
                )
            else:
                results.append(AssertionResult(passed=True, message=""))

        if resp.matches is not None:
            if re.search(resp.matches, response):
                results.append(AssertionResult(passed=True, message=""))
            else:
                results.append(
                    AssertionResult(
                        passed=False,
                        message=f"Response does not match pattern '{resp.matches}'. Got: {got}",
                    )
                )

        return results
