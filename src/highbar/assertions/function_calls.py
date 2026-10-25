"""Function calls assertion — validates tool invocations against expectations.

Each expected call needs a call of its own: two expectations of ``search`` need two
searches (A7). Which call serves which expectation is a maximum bipartite matching,
so a call that fits a narrow expectation is not spent on a broad one first. And
calling is not working: a call that failed only satisfies an expectation that
declares ``error``.
"""

from __future__ import annotations

from typing import Any

from highbar.assertions._match import subset_match, value_match
from highbar.domain.models import AssertionResult, Expect, FunctionCallExpect, Trace


def satisfies(expected: FunctionCallExpect, call: Trace) -> bool:
    """The call has an accepted name, the expected payload and the expected outcome."""
    return (
        call.fn in expected.accepted_names
        and subset_match(expected.params, call.args)
        and subset_match(expected.result, call.result)
        and _error_matches(expected.error, call.error)
    )


class FunctionCallsAssertion:
    """Evaluates whether expected function calls were made."""

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        results = [
            _failed(f"forbidden tool '{name}' was called")
            for name in expect.forbid
            if any(t.fn == name for t in traces)
        ]
        if expect.function_calls is None:
            return results

        # Guardrail: function_calls: [] means no tools should be called
        if not expect.function_calls:
            if traces:
                called = ", ".join(t.fn for t in traces)
                return [*results, _failed(f"Expected no function calls, but got: {called}")]
            return [*results, AssertionResult(passed=True, message="")]

        expected = expect.function_calls
        assignment = _assign(expected, traces)
        if None in assignment:
            # The basic problem comes first: some expected call has no call to serve it.
            return [*results, *_per_expectation(expected, traces, assignment)]
        results.extend(AssertionResult(passed=True, message="") for _ in expected)

        if expect.ordered:
            in_order = _in_order(expected, traces)
            if in_order is None:
                wanted = ", ".join(_label(e) for e in expected)
                got = ", ".join(
                    traces[c].fn for c in sorted(c for c in assignment if c is not None)
                )
                results.append(
                    _failed(f"calls matched but out of order: expected {wanted}; got {got}")
                )
            else:
                assignment = in_order

        if expect.exclusive:
            used = set(assignment)
            extra = [t.fn for c, t in enumerate(traces) if c not in used]
            if extra:
                results.append(_failed(f"unexpected calls: {', '.join(extra)}"))
        return results


def _assign(expected: list[FunctionCallExpect], traces: list[Trace]) -> list[int | None]:
    """For each expectation, the index of the call serving it (Kuhn's matching)."""
    fits = [[satisfies(e, t) for t in traces] for e in expected]
    owner: dict[int, int] = {}  # call -> expectation

    def augment(e: int, seen: set[int]) -> bool:
        for c, fit in enumerate(fits[e]):
            if not fit or c in seen:
                continue
            seen.add(c)
            if c not in owner or augment(owner[c], seen):
                owner[c] = e
                return True
        return False

    for e in range(len(expected)):
        augment(e, set())
    assignment: list[int | None] = [None] * len(expected)
    for c, e in owner.items():
        assignment[e] = c
    return assignment


def _in_order(expected: list[FunctionCallExpect], traces: list[Trace]) -> list[int | None] | None:
    """The earliest calls serving the expectations in order, or None if there are none.

    Taking the first call that fits each expectation in turn never loses a solution:
    any other choice leaves fewer calls for the expectations after it.
    """
    assignment: list[int | None] = []
    position = 0
    for e in expected:
        found = next((c for c in range(position, len(traces)) if satisfies(e, traces[c])), None)
        if found is None:
            return None
        assignment.append(found)
        position = found + 1
    return assignment


def _per_expectation(
    expected: list[FunctionCallExpect], traces: list[Trace], assignment: list[int | None]
) -> list[AssertionResult]:
    results = []
    for index, (e, call) in enumerate(zip(expected, assignment, strict=True)):
        if call is not None:
            results.append(AssertionResult(passed=True, message=""))
        else:
            results.append(_failed(_unmet(e, index, expected, traces, assignment)))
    return results


def _unmet(
    e: FunctionCallExpect,
    index: int,
    expected: list[FunctionCallExpect],
    traces: list[Trace],
    assignment: list[int | None],
) -> str:
    """Why an expected call has no call to serve it, the most basic reason first."""
    names = e.accepted_names
    candidates = [t for t in traces if t.fn in names]
    if not candidates:
        called = ", ".join(t.fn for t in traces) or "nothing"
        return f"Expected call to {_describe(names)}, but the agent called: {called}"

    if e.error is False and all(t.error is not None for t in candidates):
        return f"'{candidates[0].fn}' was called but failed: {candidates[0].error}"

    if any(satisfies(e, t) for t in candidates):
        # It fits, but another expectation needed the same call.
        wanted = sum(1 for other in expected if set(other.accepted_names) & set(names))
        return f"expected {wanted} calls to {_describe(names)}, got {len(candidates)}"

    # Calling the right tool with the wrong payload is a different defect from not
    # calling it at all, and the message has to say which one happened. The call to
    # show is one no other expectation took.
    taken = {c for c in assignment if c is not None}
    free = [t for c, t in enumerate(traces) if t.fn in names and c not in taken]
    call = (free or candidates)[0]
    errors: list[str] = []
    if e.params and not subset_match(e.params, call.args):
        errors.append(f"params: expected {e.params}, got {call.args}")
    if e.result and not subset_match(e.result, call.result):
        errors.append(f"result: expected {e.result}, got {call.result}")
    if not _error_matches(e.error, call.error):
        errors.append(f"error: expected {_expected_error(e.error)}, got {call.error or 'none'}")
    return f"'{call.fn}' was called but didn't match: {'; '.join(errors)}"


def _error_matches(expected: bool | dict[str, Any], actual: str | None) -> bool:
    if expected is False:
        return actual is None
    if actual is None:
        return False
    return expected is True or value_match(expected, actual)


def _expected_error(expected: bool | dict[str, Any]) -> str:
    if expected is True:
        return "a failure"
    if expected is False:
        return "no failure"
    return f"a failure matching {expected}"


def _failed(message: str) -> AssertionResult:
    return AssertionResult(passed=False, message=message)


def _label(e: FunctionCallExpect) -> str:
    return "/".join(e.accepted_names)


def _describe(names: list[str]) -> str:
    if len(names) == 1:
        return f"'{names[0]}'"
    alternatives = " or ".join(f"'{name}'" for name in names)
    return f"one of ({alternatives})"
