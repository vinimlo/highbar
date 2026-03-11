"""Response text assertion — validates response content."""

from __future__ import annotations

import re

from gauntlet.domain.models import AssertionResult, Expect, Trace


class ResponseAssertion:
    """Evaluates response text against expected patterns."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        if expect.response is None:
            return [AssertionResult(passed=True, message="")]

        results: list[AssertionResult] = []
        resp = expect.response

        if resp.contains is not None:
            if resp.contains in response:
                results.append(AssertionResult(passed=True, message=""))
            else:
                results.append(
                    AssertionResult(
                        passed=False,
                        message=f"Response does not contain '{resp.contains}'",
                    )
                )

        if resp.not_contains is not None:
            if re.search(resp.not_contains, response):
                results.append(
                    AssertionResult(
                        passed=False,
                        message=f"Response matches forbidden pattern '{resp.not_contains}'",
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
                        message=f"Response does not match pattern '{resp.matches}'",
                    )
                )

        if not results:
            results.append(AssertionResult(passed=True, message=""))

        return results
