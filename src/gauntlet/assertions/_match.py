"""Shared matching utilities for assertions."""

from __future__ import annotations

from typing import Any


def subset_match(expected: dict[str, Any] | None, actual: Any) -> bool:
    """Check that all expected keys are present in actual with matching values.

    Returns True if expected is None (no constraint).
    Returns False if actual is not a dict.
    """
    if expected is None:
        return True
    if not isinstance(actual, dict):
        return False
    return all(k in actual and actual[k] == v for k, v in expected.items())
