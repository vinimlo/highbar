"""Function calls assertion — validates tool invocations against expectations.

Each expected call needs a call of its own: two expectations of ``search`` need two
searches (A7). Which call serves which expectation is a maximum bipartite matching,
so a call that fits a narrow expectation is not spent on a broad one first. And
calling is not working: a call that failed only satisfies an expectation that
declares ``error``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from highbar.assertions._match import subset_match, value_match
from highbar.domain.models import AssertionResult, Expect, FunctionCallExpect, Trace

# Whether a call whose return (or arguments, HB-48) was not recorded is taken to fit
# `returns` (or `params`). Never on its own: the runner asks a second time with it on,
# and a test that only passes that way is unverified, not passed (review of #74).
_assume_unrecorded: ContextVar[bool] = ContextVar("assume_unrecorded", default=False)


def satisfies(expected: FunctionCallExpect, call: Trace) -> bool:
    """The call has an accepted name, the expected payload and the expected outcome."""
    return (
        call.fn in expected.accepted_names
        and _params_match(expected.params, call)
        and subset_match(expected.result, call.result)
        and _returns_match(expected.returns, call)
        and _error_matches(expected.error, call.error)
    )


def _params_match(expected: dict[str, Any] | None, call: Trace) -> bool:
    if expected is None:
        return True
    if not call.has_args:
        return _assume_unrecorded.get()
    return subset_match(expected, call.args)


def _returns_match(expected: Any, call: Trace) -> bool:
    if expected is None:
        return True
    if not call.has_result:
        return _assume_unrecorded.get()
    return value_match(expected, call.result)


def unrecorded_returns(traces: list[Trace], expect: Expect) -> list[str]:
    """The tools a `returns` entry names whose return some call did not record."""
    names = {
        n for e in expect.function_calls or [] if e.returns is not None for n in e.accepted_names
    }
    return sorted({t.fn for t in traces if t.fn in names and not t.has_result})


def unrecorded_args(traces: list[Trace], expect: Expect) -> list[str]:
    """The tools a `params` entry names whose arguments some call did not record (HB-48)."""
    names = {
        n for e in expect.function_calls or [] if e.params is not None for n in e.accepted_names
    }
    return sorted({t.fn for t in traces if t.fn in names and not t.has_args})


class FunctionCallsAssertion:
    """Evaluates whether expected function calls were made.

    ``assume_unrecorded``: a call whose return was not recorded fits any `returns`.
    Only the runner's second look uses it, to tell a test that could not be checked.
    """

    def __init__(self, *, assume_unrecorded: bool = False) -> None:
        self._assume_unrecorded = assume_unrecorded

    def evaluate(self, traces: list[Trace], response: str, expect: Expect) -> list[AssertionResult]:
        token = _assume_unrecorded.set(self._assume_unrecorded)
        try:
            return self._evaluate(traces, expect)
        finally:
            _assume_unrecorded.reset(token)

    def _evaluate(self, traces: list[Trace], expect: Expect) -> list[AssertionResult]:
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
                guardrail = "Expected no function calls"
                return [*results, _failed(f"{guardrail}, but got: {called}", guardrail)]
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
                results.append(_failed(*_out_of_order(expected, traces, assignment)))
            else:
                assignment = in_order

        if expect.exclusive:
            used = set(assignment)
            extra = [t.fn for c, t in enumerate(traces) if c not in used]
            if extra:
                results.append(_failed(f"unexpected calls: {', '.join(extra)}", "unexpected calls"))
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
) -> tuple[str, str]:
    wanted = [_label(e) for e in expected]
    matched = sorted(
        (c for c in assignment if c is not None), key=lambda c: (traces[c].started_at, c)
    )
    got = [traces[c].fn for c in matched]
    condition = f"calls matched but out of order: expected {', '.join(wanted)}"
    message = f"{condition}; got {', '.join(got)}"
    if got == wanted:
        message += " (they overlap: each call must start after the one before it ends)"
    return message, condition


def _per_expectation(
    expected: list[FunctionCallExpect], traces: list[Trace], assignment: list[int | None]
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    seen: set[str] = set()
    for index, (e, call) in enumerate(zip(expected, assignment, strict=True)):
        message, condition = (
            ("", None) if call is not None else _unmet(e, index, expected, traces, assignment)
        )
        # Two expectations short of the same calls say the same thing once.
        passed = not message or message in seen
        results.append(AssertionResult(passed=passed, message=message, condition=condition))
        seen.add(message)
    return results


def _unmet(
    e: FunctionCallExpect,
    index: int,
    expected: list[FunctionCallExpect],
    traces: list[Trace],
    assignment: list[int | None],
) -> tuple[str, str]:
    """Why an expected call has no call to serve it, the most basic reason first.

    With it, the condition that failed apart from what the agent did, which changes
    from run to run: a phrasing's runs are grouped by it (HB-27).
    """
    names = e.accepted_names
    candidates = [t for t in traces if t.fn in names]
    if not candidates:
        called = ", ".join(t.fn for t in traces) or "nothing"
        missing = f"Expected call to {_describe(names)}"
        return f"{missing}, but the agent called: {called}", missing

    # The condition names the expectation, the message the call: with alternatives,
    # which one the agent picked changes from run to run (review of #60).
    if e.error is False and all(t.error is not None for t in candidates):
        failed = "was called but failed"
        message = f"'{candidates[0].fn}' {failed}: {candidates[0].error}"
        return message, f"{_describe(names)} {failed}"

    if any(satisfies(e, t) for t in candidates):
        # It fits, but another expectation needed the same call.
        return _shortfall(index, expected, traces)

    # Calling the right tool with the wrong payload is a different defect from not
    # calling it at all, and the message has to say which one happened. The call to
    # show is one no other expectation took.
    taken = {c for c in assignment if c is not None}
    free = [t for c, t in enumerate(traces) if t.fn in names and c not in taken]
    call = (free or candidates)[0]
    errors: list[tuple[str, str]] = []  # what was expected, and what the call had
    if e.params and not subset_match(e.params, call.args):
        errors.append((f"params: expected {e.params}", f"{call.args}"))
    if e.result and not subset_match(e.result, call.result):
        errors.append((f"result: expected {e.result}", f"{call.result}"))
    if not _returns_match(e.returns, call):
        got = f"{call.result}" if call.has_result else "no recorded return"
        errors.append((f"returns: expected {e.returns}", got))
    if not _error_matches(e.error, call.error):
        errors.append((f"error: expected {_expected_error(e.error)}", f"{call.error or 'none'}"))
    head = "was called but didn't match: "
    message = f"'{call.fn}' {head}" + "; ".join(f"{wanted}, got {got}" for wanted, got in errors)
    return message, f"{_describe(names)} {head}" + "; ".join(wanted for wanted, _ in errors)


def _shortfall(
    index: int, expected: list[FunctionCallExpect], traces: list[Trace]
) -> tuple[str, str]:
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
    condition = f"expected {len(group)} calls to {label}"
    message = f"{condition}, got {len(fit)} that fit"
    if failed:
        message += f"; {len(failed)} more failed: " + "; ".join(t.error or "" for t in failed[:3])
    return message, condition


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


def _failed(message: str, condition: str | None = None) -> AssertionResult:
    return AssertionResult(passed=False, message=message, condition=condition)


def _label(e: FunctionCallExpect) -> str:
    return "/".join(e.accepted_names)


def _describe(names: list[str]) -> str:
    if len(names) == 1:
        return f"'{names[0]}'"
    alternatives = " or ".join(f"'{name}'" for name in names)
    return f"one of ({alternatives})"
