"""Narrowing for values read from YAML or JSON.

``isinstance(value, dict)`` on such a value tells the type checker it is a dict, but
not of what, and the unknown spreads to everything read from it. These say "a mapping
(or a list) of anything", which is what a parsed document is. Standard library only:
the SDK uses them too.
"""

from __future__ import annotations

from typing import Any, cast


def as_dict(value: object) -> dict[Any, Any] | None:
    """The value as a mapping, or ``None`` if it is not one."""
    return cast("dict[Any, Any]", value) if isinstance(value, dict) else None


def as_list(value: object) -> list[Any] | None:
    """The value as a list, or ``None`` if it is not one."""
    return cast("list[Any]", value) if isinstance(value, list) else None
