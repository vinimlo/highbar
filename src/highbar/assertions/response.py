"""Response text assertion — the reply checked with the matchers of ``params`` (D8)."""

from __future__ import annotations

import re
from typing import Any

from highbar.assertions._match import value_match
from highbar.domain.models import AssertionResult, Expect, Trace
from highbar.engine.secrets import ELLIPSIS


def truncate(text: str, limit: int = 300) -> str:
    """Cut ``text`` at ``limit`` characters, ending with an ellipsis only when it cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + ELLIPSIS


class ResponseAssertion:
    """Evaluates the response text: one result per condition of its matcher."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        if expect.response is None:
            return []

        # What the agent said is the first thing anyone asks when this fails.
        got = repr(truncate(response))
        results: list[AssertionResult] = []
        for key, operand in expect.response.matcher.items():
            if value_match({key: operand}, response):
                results.append(AssertionResult(passed=True, message=""))
            else:
                condition, found = _why(key, operand, response)
                message = f"{condition}{found}. Got: {got}"
                results.append(AssertionResult(passed=False, message=message, condition=condition))
        return results


def _why(key: str, operand: Any, response: str) -> tuple[str, str]:
    """The condition that failed, and what of the reply it found, if anything."""
    if key in ("not_matches", "not_imatches"):
        ignore_case = key == "not_imatches"
        found = re.search(str(operand), response, re.IGNORECASE if ignore_case else 0)
        snippet = found.group(0) if found else ""
        how = " (ignoring case)" if ignore_case else ""
        condition = f"Response matches forbidden pattern '{operand}'{how}"
        return condition, f" (found '{truncate(snippet, 80)}')"
    return {
        "contains": f"Response does not contain '{operand}'",
        "icontains": f"Response does not contain '{operand}' (ignoring case)",
        "not_contains": f"Response contains forbidden text '{operand}'",
        "not_icontains": f"Response contains forbidden text '{operand}' (ignoring case)",
        "matches": f"Response does not match pattern '{operand}'",
        "imatches": f"Response does not match pattern '{operand}' (ignoring case)",
        "equals": f"Response is not '{operand}'",
    }[key], ""
