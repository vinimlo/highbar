"""Shared matching utilities for assertions."""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable
from typing import Any, cast

from highbar._narrow import as_dict, as_list


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


def _number(value: Any) -> int | float | None:
    """The value as a number, or None when it is not one.

    A bool is left out although Python counts it as an int (``True < 3``), and so is a
    numeric string: a model that writes ``"2"`` where the tool takes an integer has made
    the mistake a bound is there to catch.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _bound(holds: Callable[[Any, Any], Any]) -> Callable[[Any, Any], bool]:
    def check(actual: Any, operand: Any) -> bool:
        # A value that is not a number fails, as a negative fails on a value with no text.
        number = _number(actual)
        return number is not None and bool(holds(number, operand))

    return check


def _length(holds: Callable[[Any, Any], Any]) -> Callable[[Any, Any], bool]:
    def check(actual: Any, operand: Any) -> bool:
        # Lists only: a string has a length too, but a list that arrived as a string is
        # the defect, and an object's length is its number of keys, which nobody means.
        if not isinstance(actual, (list, tuple)):
            return False
        items = cast("list[Any] | tuple[Any, ...]", actual)
        return bool(holds(len(items), operand))

    return check


def _one_of(actual: Any, operand: Any) -> bool:
    # The same comparison as `equals`, against each listed value.
    return any(bool(actual == option) for option in operand)


def _contains_all(actual: Any, operand: Any) -> bool:
    return all(_contains(actual, item, ignore_case=False) is True for item in operand)


def _items(actual: Any) -> list[Any] | None:
    if not isinstance(actual, (list, tuple)):
        return None
    return list(cast("list[Any] | tuple[Any, ...]", actual))


def _any_item(actual: Any, operand: Any) -> bool:
    items = _items(actual)
    return items is not None and any(value_match(operand, item) for item in items)


def _every_item(actual: Any, operand: Any) -> bool:
    # An empty list fails: every item of nothing matched, and nothing was checked. Python's
    # all([]) is True, which here would be a green with no evidence behind it.
    items = _items(actual)
    return bool(items) and all(value_match(operand, item) for item in items or ())


# Mirrors the vocabulary `response:` already uses (contains / not_contains / matches),
# with the same case-sensitive semantics, so a spec author doesn't have to learn a second
# dialect. The `i` variants ignore case. `equals` is the escape hatch for a literal value
# that happens to look like a matcher.
_TEXT_CONDITIONS: dict[str, Callable[[Any, Any], bool]] = {
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

# Bounds on a number and on the length of a list (HB-7): "at most 3 documents", "up to
# 6000 tokens". Only values of params and result have them; a reply and an error are text.
_CONDITIONS: dict[str, Callable[[Any, Any], bool]] = {
    **_TEXT_CONDITIONS,
    "lt": _bound(operator.lt),
    "lte": _bound(operator.le),
    "gt": _bound(operator.gt),
    "gte": _bound(operator.ge),
    "len": _length(operator.eq),
    "len_lte": _length(operator.le),
    # One of a list of values, all of a list of pieces, and the items of a list matched
    # one by one: "some document has id X and chunks [0, 1, 2]".
    "in": _one_of,
    "contains_all": _contains_all,
    "any": _any_item,
    "all": _every_item,
}

MATCHER_KEYS = frozenset(_CONDITIONS)
# What `response` and `error` accept: the matchers that read text.
TEXT_MATCHER_KEYS = frozenset(_TEXT_CONDITIONS)
# Their operand is a regex, so a spec has to find out it is broken before a run does.
REGEX_KEYS = ("matches", "imatches", "not_matches", "not_imatches")
NUMBER_KEYS = ("lt", "lte", "gt", "gte")
LENGTH_KEYS = ("len", "len_lte")
# Their operand is a list of values: an empty one would make a check that cannot hold
# (`in`) or cannot fail (`contains_all`).
LIST_OPERAND_KEYS = ("in", "contains_all")
# Their operand is an expected value for each item, matchers included.
ITEM_KEYS = ("any", "all")


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

    for key in NUMBER_KEYS:
        if key not in mapping:
            continue
        bound = mapping[key]
        if _number(bound) is None:
            msg = f"{where}: '{key}' needs a number, got {type(bound).__name__}"
            raise ValueError(msg)
        if isinstance(bound, float) and not math.isfinite(bound):
            # `lte: .inf` holds for every number, and nothing is ever below `.nan`. An int
            # is always finite, and math.isfinite would overflow on a huge one.
            msg = f"{where}: '{key}' needs a finite number, got {bound!r}"
            raise ValueError(msg)

    for key in LENGTH_KEYS:
        if key not in mapping:
            continue
        size = mapping[key]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            msg = f"{where}: '{key}' needs a whole number, 0 or more, got {size!r}"
            raise ValueError(msg)

    for key in LIST_OPERAND_KEYS:
        if key in mapping and not as_list(mapping[key]):
            msg = f"{where}: '{key}' needs a non-empty list, got {mapping[key]!r}"
            raise ValueError(msg)
    if "contains_all" in mapping and "" in mapping["contains_all"]:
        # "" is in every text: one piece that can never fail.
        msg = f"{where}: 'contains_all' has an empty text, which every value contains"
        raise ValueError(msg)

    for key in ITEM_KEYS:
        if key not in mapping:
            continue
        if as_dict(mapping[key]) == {}:
            # `all: {}` subset-matches nothing, so it only says "these are objects".
            msg = f"{where}: '{key}' needs something to match in each item, got {{}}"
            raise ValueError(msg)
        validate_expected(mapping[key], f"{where}.{key}")
