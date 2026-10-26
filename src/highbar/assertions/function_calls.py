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
                results.append(_failed(_out_of_order(expected, traces, assignment)))
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
    """Calls serving the expectations one after the other, or None if there are none.

    In order means by when the calls ran: each starts after the one before it ended.
    The file lists calls as they end, so an async call that started first and ended
    last would otherwise pass for second. Taking, for each expectation, the fitting
    call that ends first never loses a solution: it leaves the most time for the
    expectations after it.
    """
    used: set[int] = set()
    previous_end = float("-inf")
    assignment: list[int | None] = []
    for e in expected:
        fits = [
            c
            for c, t in enumerate(traces)
            if c not in used and t.started_at >= previous_end and satisfies(e, t)
        ]
        if not fits:
            return None
        chosen = min(fits, key=lambda c: (traces[c].ended_at, c))
        used.add(chosen)
        previous_end = traces[chosen].ended_at
        assignment.append(chosen)
    return assignment


def _out_of_order(
    expected: list[FunctionCallExpect], traces: list[Trace], assignment: list[int | None]
) -> str:
    wanted = [_label(e) for e in expected]
    matched = sorted(
        (c for c in assignment if c is not None), key=lambda c: (traces[c].started_at, c)
    )
    got = [traces[c].fn for c in matched]
    message = f"calls matched but out of order: expected {', '.join(wanted)}; got {', '.join(got)}"
    if got == wanted:
        message += " (they overlap: each call must start after the one before it ends)"
    return message


def _per_expectation(
    expected: list[FunctionCallExpect], traces: list[Trace], assignment: list[int | None]
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    seen: set[str] = set()
    for index, (e, call) in enumerate(zip(expected, assignment, strict=True)):
        message = "" if call is not None else _unmet(e, index, expected, traces, assignment)
        # Two expectations short of the same calls say the same thing once.
        results.append(AssertionResult(passed=not message or message in seen, message=message))
        seen.add(message)
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
        return _shortfall(index, expected, traces)

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


def _shortfall(index: int, expected: list[FunctionCallExpect], traces: list[Trace]) -> str:
    """How many calls the expectations sharing names with this one need, and got.

    Sharing is transitive: with [a, a|b, b], all three compete for calls to a and b.
    A call counts as got only if it fits one of them; failed ones are said apart.
    """
    group = {index}
    names: list[str] = list(expected[index].accepted_names)
    grew = True
    while grew:
        grew = False
        for other, e in enumerate(expected):
            if other not in group and set(e.accepted_names) & set(names):
                group.add(other)
                names.extend(n for n in e.accepted_names if n not in names)
                grew = True
    # Said in the spec's order, whichever expectation was found short.
    names = list(dict.fromkeys(n for g in sorted(group) for n in expected[g].accepted_names))
    fit = [t for t in traces if any(satisfies(expected[g], t) for g in group)]
    failed = [t for t in traces if t.fn in names and t.error is not None and t not in fit]
    label = " or ".join(f"'{name}'" for name in names)
    message = f"expected {len(group)} calls to {label}, got {len(fit)} that fit"
    if failed:
        message += f"; {len(failed)} more failed: " + "; ".join(t.error or "" for t in failed[:3])
    return message


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
