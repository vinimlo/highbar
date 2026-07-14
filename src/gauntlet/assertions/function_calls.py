"""Function calls assertion — validates tool invocations against expectations."""

from __future__ import annotations

from gauntlet.assertions._match import subset_match
from gauntlet.domain.models import AssertionResult, Expect, FunctionCallExpect, Trace


class FunctionCallsAssertion:
    """Evaluates whether expected function calls were made."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        if expect.function_calls is None:
            return []

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
        params_ok = subset_match(expected.params, trace.args)
        result_ok = subset_match(expected.result, trace.result)

        if params_ok and result_ok:
            return AssertionResult(passed=True, message="")

    # Build detailed error message
    errors: list[str] = []
    trace = matching_traces[0]

    if expected.params and not subset_match(expected.params, trace.args):
        errors.append(f"params: expected {expected.params}, got {trace.args}")
    if expected.result and not subset_match(expected.result, trace.result):
        errors.append(f"result: expected {expected.result}, got {trace.result}")

    return AssertionResult(
        passed=False,
        message=f"'{expected.name}' was called but didn't match: {'; '.join(errors)}",
    )
