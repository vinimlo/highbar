"""Function calls assertion — validates tool invocations against expectations."""

from __future__ import annotations

from highbar.assertions._match import subset_match
from highbar.domain.models import AssertionResult, Expect, FunctionCallExpect, Trace


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
    """Find a trace that matches the expected function call.

    When ``name`` lists several tools, any one of them satisfies the expectation.
    """
    names = expected.accepted_names
    matching_traces = [t for t in traces if t.fn in names]

    if not matching_traces:
        called = ", ".join(t.fn for t in traces) or "nothing"
        return AssertionResult(
            passed=False,
            message=f"Expected call to {_describe(names)}, but the agent called: {called}",
        )

    for trace in matching_traces:
        params_ok = subset_match(expected.params, trace.args)
        result_ok = subset_match(expected.result, trace.result)

        if params_ok and result_ok:
            return AssertionResult(passed=True, message="")

    # Calling the right tool with the wrong payload is a different defect from not
    # calling it at all, and the message has to say which one happened.
    errors: list[str] = []
    trace = matching_traces[0]

    if expected.params and not subset_match(expected.params, trace.args):
        errors.append(f"params: expected {expected.params}, got {trace.args}")
    if expected.result and not subset_match(expected.result, trace.result):
        errors.append(f"result: expected {expected.result}, got {trace.result}")

    return AssertionResult(
        passed=False,
        message=f"'{trace.fn}' was called but didn't match: {'; '.join(errors)}",
    )


def _describe(names: list[str]) -> str:
    if len(names) == 1:
        return f"'{names[0]}'"
    alternatives = " or ".join(f"'{name}'" for name in names)
    return f"one of ({alternatives})"
