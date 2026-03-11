"""Function calls assertion — validates tool invocations against expectations."""

from __future__ import annotations

from typing import Any

from gauntlet.domain.models import AssertionResult, Expect, FunctionCallExpect, Trace


class FunctionCallsAssertion:
    """Evaluates whether expected function calls were made."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        if expect.function_calls is None:
            return [AssertionResult(passed=True, message="")]

        # Guardrail: function_calls: [] means no tools should be called
        if len(expect.function_calls) == 0:
            if len(traces) > 0:
                called = ", ".join(t.fn for t in traces)
                return [
                    AssertionResult(
                        passed=False,
                        message=f"Expected no function calls, but got: {called}",
                    )
                ]
            return [AssertionResult(passed=True, message="")]

        results: list[AssertionResult] = []
        for expected_call in expect.function_calls:
            result = _match_call(expected_call, traces)
            results.append(result)

        return results


def _match_call(expected: FunctionCallExpect, traces: list[Trace]) -> AssertionResult:
    """Find a trace that matches the expected function call."""
    matching_traces = [t for t in traces if t.fn == expected.name]

    if not matching_traces:
        return AssertionResult(
            passed=False,
            message=f"Expected call to '{expected.name}', but it was never called",
        )

    for trace in matching_traces:
        params_ok = _check_params(expected.params, trace.args)
        result_ok = _check_result(expected.result, trace.result)

        if params_ok and result_ok:
            return AssertionResult(passed=True, message="")

    # Build detailed error message
    errors: list[str] = []
    trace = matching_traces[0]

    if expected.params and not _check_params(expected.params, trace.args):
        errors.append(f"params: expected {expected.params}, got {trace.args}")
    if expected.result and not _check_result(expected.result, trace.result):
        errors.append(f"result: expected {expected.result}, got {trace.result}")

    return AssertionResult(
        passed=False,
        message=f"'{expected.name}' was called but didn't match: {'; '.join(errors)}",
    )


def _check_params(expected: dict[str, Any] | None, actual: dict[str, Any]) -> bool:
    """Subset match — all expected keys must be present with matching values."""
    if expected is None:
        return True
    return all(k in actual and actual[k] == v for k, v in expected.items())


def _check_result(expected: dict[str, Any] | None, actual: Any) -> bool:
    """Subset match on result — all expected keys must be present."""
    if expected is None:
        return True
    if not isinstance(actual, dict):
        return False
    return all(k in actual and actual[k] == v for k, v in expected.items())
