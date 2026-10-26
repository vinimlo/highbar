"""Placeholders in the request, the correlation and the session hooks (spec D5).

``{{message}}`` is the trigger being sent, ``{{test.id}}`` the id of this test
execution (also sent as ``X-Highbar-Test``), ``{{test.name}}`` the test's name, and
``{{session.<path>}}`` a value from the object the session setup returned.

A value that is only a placeholder keeps the JSON type of what it resolves to: an
integer ``conversation_id`` goes out as an integer. Inside a longer text it is
interpolated, as JSON when it is not text. ``{{previous.*}}`` belongs to triggers and
is not resolved here.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from highbar.engine.paths import PathError, get_path
from highbar.engine.schema import SpecError

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

ALL = frozenset({"message", "test.id", "test.name", "session"})
# Hooks run outside a message: no {{message}}; setup runs before the session exists.
SETUP = frozenset({"test.id", "test.name"})
TEARDOWN = frozenset({"test.id", "test.name", "session"})


@dataclass(frozen=True)
class Values:
    message: str | None
    test_id: str
    test_name: str
    session: Mapping[str, Any]


class PlaceholderError(ValueError):
    """A placeholder with no value at run time (a session key that is not there)."""


def _kind(name: str) -> str:
    return "session" if name.startswith("session.") else name


def check_placeholders(
    value: Any, where: str, *, allowed: frozenset[str], has_session: bool
) -> None:
    """Reject placeholders that are unknown, or not available where they are used."""
    if isinstance(value, dict):
        for key, item in value.items():
            check_placeholders(item, f"{where}.{key}", allowed=allowed, has_session=has_session)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            check_placeholders(item, f"{where}[{index}]", allowed=allowed, has_session=has_session)
        return
    if not isinstance(value, str):
        return
    for match in PLACEHOLDER.finditer(value):
        name = match.group(1)
        kind = _kind(name)
        if kind not in ALL or (kind == "session" and name == "session."):
            known = ["message", "test.id", "test.name", "session.<key>"]
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
            hint = f" — did you mean '{{{{{close[0]}}}}}'?" if close else ""
            raise SpecError(
                f"{where}: unknown placeholder '{match.group(0)}'{hint} "
                "(placeholders here: {{message}}, {{test.id}}, {{test.name}}, {{session.<key>}})"
            )
        if kind not in allowed:
            raise SpecError(f"{where}: '{match.group(0)}' is not available here")
        if kind == "session" and not has_session:
            raise SpecError(
                f"{where}: '{match.group(0)}' needs agent.session, whose setup returns it"
            )


def uses_placeholder(value: Any, name: str) -> bool:
    if isinstance(value, dict):
        return any(uses_placeholder(item, name) for item in value.values())
    if isinstance(value, list):
        return any(uses_placeholder(item, name) for item in value)
    return isinstance(value, str) and any(
        match.group(1) == name for match in PLACEHOLDER.finditer(value)
    )


def render(value: Any, values: Values) -> Any:
    """Resolve every placeholder in ``value`` (texts, and texts inside maps and lists)."""
    if isinstance(value, dict):
        return {key: render(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, values) for item in value]
    if not isinstance(value, str):
        return value

    whole = PLACEHOLDER.fullmatch(value)
    if whole is not None:
        return _resolve(whole.group(1), values)

    def text(match: re.Match[str]) -> str:
        resolved = _resolve(match.group(1), values)
        if isinstance(resolved, str):
            return resolved
        return json.dumps(resolved, ensure_ascii=False)

    return PLACEHOLDER.sub(text, value)


def _resolve(name: str, values: Values) -> Any:
    if name == "message":
        if values.message is None:
            raise PlaceholderError("{{message}}: there is no message here")
        return values.message
    if name == "test.id":
        return values.test_id
    if name == "test.name":
        return values.test_name
    path = name.removeprefix("session.")
    try:
        return get_path(dict(values.session), path)
    except PathError as e:
        raise PlaceholderError(f"{{{{session.{path}}}}}: {e}") from None
