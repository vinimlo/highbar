"""Spec schema — every key and every type is checked before anything runs.

A misspelled key used to be skipped, and a test whose checks were all misspelled ran
and passed: green for something nobody looked at. Here an unknown key is an error that
names where it is and what was probably meant.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from typing import Any

from highbar.assertions._match import MATCHER_KEYS


class SpecError(ValueError):
    """A spec that cannot run as written. The message is always ``<where>: <problem>``."""


ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "spec": frozenset({"agent", "suites", "defaults"}),
    "defaults": frozenset({"runs", "min_pass"}),
    "agent": frozenset(
        {
            "start",
            "endpoint",
            "health",
            "startup_timeout",
            "settle_time",
            "timeout",
            "request",
            "response",
            "traces",
            "session",
            "correlate",
        }
    ),
    "request": frozenset({"method", "headers", "body"}),
    "session": frozenset({"setup", "teardown"}),
    "python_hook": frozenset({"python"}),
    "suite": frozenset({"name", "tests"}),
    "test": frozenset({"name", "trigger", "expect", "turns", "variations", "runs", "min_pass"}),
    "turn": frozenset({"name", "trigger", "expect"}),
    "expect": frozenset({"function_calls", "response", "ordered", "exclusive", "forbid"}),
    "function_call": frozenset({"name", "params", "result", "error"}),
    "response": MATCHER_KEYS,
}

# Keys starting with this are the author's own (YAML anchors, notes), as in compose
# files and OpenAPI. They are skipped at every level.
EXTENSION_PREFIX = "x-"


def check_keys(raw: Mapping[str, Any], kind: str, where: str) -> None:
    """Reject any key that ``kind`` does not define, suggesting the closest one."""
    allowed = ALLOWED_KEYS[kind]
    for key in raw:
        name = str(key)
        if name in allowed or name.startswith(EXTENSION_PREFIX):
            continue
        close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
        if close:
            msg = f"{where}: unknown key '{name}' — did you mean '{close[0]}'?"
        else:
            msg = f"{where}: unknown key '{name}' (allowed: {', '.join(sorted(allowed))})"
        raise SpecError(msg)


def require_type(value: Any, expected: type | tuple[type, ...], where: str, what: str) -> None:
    """Raise ``<where>: <what>, got <type>`` unless ``value`` is of ``expected`` type.

    ``bool`` is not accepted as an ``int``: ``startup_timeout: yes`` is a mistake, not 1.
    """
    types = expected if isinstance(expected, tuple) else (expected,)
    is_bool_for_int = isinstance(value, bool) and bool not in types
    if is_bool_for_int or not isinstance(value, types):
        raise SpecError(f"{where}: {what}, got {type(value).__name__}")


def user_keys(raw: Mapping[str, Any]) -> list[str]:
    """The keys that carry meaning, without the ``x-`` extension keys."""
    return [str(key) for key in raw if not str(key).startswith(EXTENSION_PREFIX)]
