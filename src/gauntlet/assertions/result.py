"""Result assertion — validates tool return values against expectations."""

from __future__ import annotations

from typing import Any

from gauntlet.domain.models import AssertionResult, Expect, FunctionCallExpect, Trace


class ResultAssertion:
    """Evaluates tool return values against expected results."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        if expect.function_calls is None:
            return [AssertionResult(passed=True, message="")]

        results: list[AssertionResult] = []
        for fc_expect in expect.function_calls:
            if fc_expect.result is None:
                continue
            result = _check_result_for_call(fc_expect, traces)
            results.append(result)

        if not results:
            return [AssertionResult(passed=True, message="")]

        return results


def _check_result_for_call(expected: FunctionCallExpect, traces: list[Trace]) -> AssertionResult:
    matching = [t for t in traces if t.fn == expected.name]

    if not matching:
        return AssertionResult(
            passed=False,
            message=f"Cannot check result: '{expected.name}' was never called",
        )

    for trace in matching:
        if _subset_match(expected.result, trace.result):
            return AssertionResult(passed=True, message="")

    actual = matching[0].result
    return AssertionResult(
        passed=False,
        message=f"'{expected.name}' result mismatch: expected {expected.result}, got {actual}",
    )


def _subset_match(expected: dict[str, Any] | None, actual: Any) -> bool:
    if expected is None:
        return True
    if not isinstance(actual, dict):
        return False
    return all(k in actual and actual[k] == v for k, v in expected.items())
