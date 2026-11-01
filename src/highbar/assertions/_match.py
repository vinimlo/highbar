"""Shared matching utilities for assertions."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast

from highbar._narrow import as_dict


def subset_match(expected: dict[str, Any] | None, actual: Any) -> bool:
    """Check that all expected keys are present in actual with matching values.

    Returns True if expected is None (no constraint).
    Returns False if actual is not a dict.
    """
    if expected is None:
        return True
    if not isinstance(actual, dict):
        return False
    return all(k in actual and value_match(v, actual[k]) for k, v in expected.items())


def value_match(expected: Any, actual: Any) -> bool:
    """Compare one expected value against the actual one.

    A plain value compares by equality. A nested dict is subset-matched, so matchers
    work at any depth.

    A dict whose keys are ALL matcher keys is treated as a matcher instead:

        params:
          product_name: {icontains: "coca"}    # case-insensitive substring
          order_id:     {matches: "^[0-9]+$"}  # regex

    This exists because tool arguments are written by a language model, and case is
    the model's whim, not semantics: the same agent writes `coca-cola` on one run and
    `Coca-Cola` on the next. Exact equality turns that into a red test with no defect
    behind it — and a red test with no defect behind it teaches the team to ignore red.

    To compare against a literal dict that happens to look like a matcher, wrap it:
    ``{equals: {contains: "x"}}``.
    """
    if _is_matcher(expected):
        return _apply_matcher(expected, actual)
    expected_dict, actual_dict = as_dict(expected), as_dict(actual)
    if expected_dict is not None and actual_dict is not None:
        return subset_match(expected_dict, actual_dict)
    return bool(expected == actual)


def _as_text(actual: Any) -> str | None:
    """Text form of a scalar, or None when the value has no meaningful text form.

    None and containers are deliberately left out: ``str(None)`` is ``"None"``, which
    happily matches ``^[a-zA-Z]+$``, and a dict's repr contains its keys.
    """
    if isinstance(actual, str):
        return actual
    if isinstance(actual, (int, float)):
        return str(actual)
    return None


def _contains(actual: Any, operand: Any, *, ignore_case: bool) -> bool | None:
    """Substring for scalars, membership for lists. None when it cannot be evaluated."""
    if isinstance(actual, (list, tuple)):
        items: list[Any] = list(cast("list[Any] | tuple[Any, ...]", actual))
        if ignore_case and isinstance(operand, str):
            folded = operand.casefold()
            return any(isinstance(item, str) and item.casefold() == folded for item in items)
        return operand in items

    text = _as_text(actual)
    if text is None:
        return None
    needle = str(operand)
    if ignore_case:
        return needle.casefold() in text.casefold()
    return needle in text


def _positive(ignore_case: bool) -> Callable[[Any, Any], bool]:
    return lambda actual, operand: _contains(actual, operand, ignore_case=ignore_case) is True


def _negative(ignore_case: bool) -> Callable[[Any, Any], bool]:
    # `is False`, not `not ...`: a negative assertion that could not be evaluated (the
    # value is None or a dict) must fail, otherwise it is green without checking anything.
    return lambda actual, operand: _contains(actual, operand, ignore_case=ignore_case) is False


def _found(actual: Any, operand: Any, *, ignore_case: bool) -> bool | None:
    """Whether the regex is in the value. None when the value has no text form."""
    text = _as_text(actual)
    if text is None:
        return None
    return re.search(str(operand), text, re.IGNORECASE if ignore_case else 0) is not None


def _regex_positive(ignore_case: bool) -> Callable[[Any, Any], bool]:
    return lambda actual, operand: _found(actual, operand, ignore_case=ignore_case) is True


def _regex_negative(ignore_case: bool) -> Callable[[Any, Any], bool]:
    # Like the other negatives, a value with no text form fails rather than passing.
    return lambda actual, operand: _found(actual, operand, ignore_case=ignore_case) is False


# Mirrors the vocabulary `response:` already uses (contains / not_contains / matches),
# with the same case-sensitive semantics, so a spec author doesn't have to learn a second
# dialect. The `i` variants ignore case. `equals` is the escape hatch for a literal value
# that happens to look like a matcher.
_CONDITIONS: dict[str, Callable[[Any, Any], bool]] = {
    "equals": lambda actual, operand: bool(actual == operand),
    "contains": _positive(ignore_case=False),
    "icontains": _positive(ignore_case=True),
    "not_contains": _negative(ignore_case=False),
    "not_icontains": _negative(ignore_case=True),
    "matches": _regex_positive(ignore_case=False),
    "imatches": _regex_positive(ignore_case=True),
    "not_matches": _regex_negative(ignore_case=False),
    "not_imatches": _regex_negative(ignore_case=True),
}

MATCHER_KEYS = frozenset(_CONDITIONS)
# Their operand is a regex, so a spec has to find out it is broken before a run does.
REGEX_KEYS = ("matches", "imatches", "not_matches", "not_imatches")


def _is_matcher(expected: Any) -> bool:
    mapping = as_dict(expected)
    return mapping is not None and len(mapping) > 0 and set(mapping) <= MATCHER_KEYS


def _apply_matcher(matcher: dict[str, Any], actual: Any) -> bool:
    """Every condition in the matcher must hold."""
    return all(_CONDITIONS[key](actual, operand) for key, operand in matcher.items())


def _stem(key: str) -> str:
    """Fold the slips people actually make: case, hyphens, a dropped or extra plural."""
    stem = key.lower().replace("-", "_")
    for suffix in ("es", "s"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


# `contain`, `Matches`, `not-contains`... A dict keyed by one of these is not a matcher,
# so without this check it would silently be compared as a literal.
_NEAR_MISSES = {_stem(key): key for key in MATCHER_KEYS}


def validate_expected(expected: Any, where: str) -> None:
    """Reject expected values whose matchers are malformed or ambiguous.

    Called by the parser so that a bad matcher fails at `highbar validate` instead of
    blowing up — or, worse, silently comparing as a literal — in the middle of a run.

    ``where`` names the field being checked (e.g. ``params.order_id``) for the message.
    """
    mapping = as_dict(expected)
    if mapping is None:
        return

    keys = [str(key) for key in mapping]
    known = [key for key in keys if key in MATCHER_KEYS]
    unknown = [key for key in keys if key not in MATCHER_KEYS]

    if known and unknown:
        msg = (
            f"{where}: mixes matcher keys {sorted(known)} with other keys {sorted(unknown)}. "
            "Use only matcher keys, or wrap a literal dict as {equals: {...}}"
        )
        raise ValueError(msg)

    if not known:
        for key in unknown:
            intended = _NEAR_MISSES.get(_stem(key))
            if intended:
                msg = (
                    f"{where}: unknown matcher '{key}' — did you mean '{intended}'? "
                    "For a literal dict with this key, wrap it as {equals: {...}}"
                )
                raise ValueError(msg)
        for key, value in mapping.items():
            validate_expected(value, f"{where}.{key}")
        return

    # A matcher. The operand of `equals` is a literal and is deliberately not inspected.
    for key in REGEX_KEYS:
        if key not in mapping:
            continue
        pattern = mapping[key]
        if not isinstance(pattern, str):
            msg = f"{where}: '{key}' needs a regex string, got {type(pattern).__name__}"
            raise ValueError(msg)
        try:
            re.compile(pattern)
        except re.error as exc:
            msg = f"{where}: invalid regex in '{key}' ({pattern!r}): {exc}"
            raise ValueError(msg) from exc
