"""Placeholders in the request, the correlation and the session hooks (spec D5).

``{{message}}`` is the trigger being sent, ``{{test.id}}`` the id of this test
execution (also sent as ``X-Highbar-Test``), ``{{test.name}}`` the test's name,
``{{test.args.<path>}}`` a value of the test's own ``session: {args: ...}`` (HB-9),
``{{test.history}}`` its ``history`` as a list of ``{role, content}`` (HB-24),
``{{session.<path>}}`` a value from the object the session setup returned, and
``{{run.<path>}}`` one from the object ``agent.run.setup`` returned (HB-10).

A value that is only a placeholder keeps the JSON type of what it resolves to: an
integer ``conversation_id`` goes out as an integer. Inside a longer text it is
interpolated, as JSON when it is not text. ``{{previous.*}}`` belongs to triggers and
is not resolved here.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from highbar._narrow import as_dict, as_list
from highbar.engine.paths import PathError, get_path
from highbar.engine.schema import SpecError
from highbar.engine.templates import no_path_message

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

ALL = frozenset({"message", "test.id", "test.name", "test.args", "test.history", "session", "run"})
# Hooks run outside a message: no {{message}}; setup runs before the session exists.
# A test's args are known before it: setup can use them. The run's values are known
# before any test.
SETUP = frozenset({"test.id", "test.name", "test.args", "test.history", "run"})
TEARDOWN = frozenset({"test.id", "test.name", "test.args", "test.history", "session", "run"})
# agent.run.teardown belongs to no test: it reads only what agent.run.setup returned.
RUN_TEARDOWN = frozenset({"run"})


@dataclass(frozen=True)
class Values:
    message: str | None
    test_id: str
    test_name: str
    session: Mapping[str, Any]
    args: Mapping[str, Any] = field(default_factory=dict[str, Any])
    history: Sequence[Mapping[str, str]] = ()
    run: Mapping[str, Any] = field(default_factory=dict[str, Any])


class PlaceholderError(ValueError):
    """A placeholder with no value at run time (a session key that is not there)."""


def _kind(name: str) -> str:
    if name.startswith("session."):
        return "session"
    if name.startswith("run."):
        return "run"
    return "test.args" if name.startswith("test.args.") else name


def args_used(value: Any, where: str) -> list[tuple[str, str]]:
    """Each ``{{test.args.<path>}}`` in ``value``: where it is, and its path."""
    if (mapping := as_dict(value)) is not None:
        return [use for key, item in mapping.items() for use in args_used(item, f"{where}.{key}")]
    if (items := as_list(value)) is not None:
        return [use for i, item in enumerate(items) for use in args_used(item, f"{where}[{i}]")]
    if not isinstance(value, str):
        return []
    return [
        (where, name.removeprefix("test.args."))
        for name in (match.group(1) for match in PLACEHOLDER.finditer(value))
        if _kind(name) == "test.args" and name != "test.args."
    ]


def check_placeholders(
    value: Any, where: str, *, allowed: frozenset[str], has_session: bool, has_run: bool = False
) -> None:
    """Reject placeholders that are unknown, or not available where they are used."""
    if (mapping := as_dict(value)) is not None:
        for key, item in mapping.items():
            check_placeholders(
                item, f"{where}.{key}", allowed=allowed, has_session=has_session, has_run=has_run
            )
        return
    if (items := as_list(value)) is not None:
        for index, item in enumerate(items):
            check_placeholders(
                item, f"{where}[{index}]", allowed=allowed, has_session=has_session, has_run=has_run
            )
        return
    if not isinstance(value, str):
        return
    for match in PLACEHOLDER.finditer(value):
        name = match.group(1)
        if name in ("session", "run"):
            # As in a trigger. It loaded, and only failed when the request was built.
            raise SpecError(f"{where}: '{{{{{name}}}}}' {no_path_message(name)}")
        kind = _kind(name)
        if kind not in ALL or name in ("session.", "run.", "test.args", "test.args."):
            known = [
                "message",
                "test.id",
                "test.name",
                "test.args.<key>",
                "test.history",
                "session.<key>",
                "run.<key>",
            ]
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
            hint = f" — did you mean '{{{{{close[0]}}}}}'?" if close else ""
            raise SpecError(
                f"{where}: unknown placeholder '{match.group(0)}'{hint} "
                "(placeholders here: {{message}}, {{test.id}}, {{test.name}}, "
                "{{test.args.<key>}}, {{test.history}}, {{session.<key>}}, {{run.<key>}})"
            )
        if kind not in allowed:
            raise SpecError(f"{where}: '{match.group(0)}' is not available here")
        if kind == "session" and not has_session:
            raise SpecError(
                f"{where}: '{match.group(0)}' needs agent.session, whose setup returns it"
            )
        if kind == "run" and not has_run:
            raise SpecError(
                f"{where}: '{match.group(0)}' needs agent.run with a setup, which returns it"
            )


def owned(text: str) -> list[str]:
    """The placeholders highbar resolves somewhere, as written in ``text``.

    ``{{.ID}}`` in a ``docker --format`` is not one of them, and stays text.
    """
    return [
        match.group(0)
        for match in PLACEHOLDER.finditer(text)
        if _kind(match.group(1)) in ALL or match.group(1) in ("session", "run")
    ]


def reads(value: Any, kind: str) -> bool:
    """Whether ``value`` has a placeholder of ``kind`` (``run`` for ``{{run.<key>}}``)."""
    if (mapping := as_dict(value)) is not None:
        return any(reads(item, kind) for item in mapping.values())
    if (items := as_list(value)) is not None:
        return any(reads(item, kind) for item in items)
    return isinstance(value, str) and any(
        _kind(match.group(1)) == kind for match in PLACEHOLDER.finditer(value)
    )


def uses_placeholder(value: Any, name: str) -> bool:
    if (mapping := as_dict(value)) is not None:
        return any(uses_placeholder(item, name) for item in mapping.values())
    if (items := as_list(value)) is not None:
        return any(uses_placeholder(item, name) for item in items)
    return isinstance(value, str) and any(
        match.group(1) == name for match in PLACEHOLDER.finditer(value)
    )


def render(value: Any, values: Values) -> Any:
    """Resolve every placeholder in ``value`` (texts, and texts inside maps and lists)."""
    if (mapping := as_dict(value)) is not None:
        return {key: render(item, values) for key, item in mapping.items()}
    if (items := as_list(value)) is not None:
        return [render(item, values) for item in items]
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


def render_url(template: str, values: Values) -> str:
    """Resolve the placeholders of a URL, each value percent-encoded as one piece.

    A value is data in the URL, as it is JSON in a body: a ``/``, ``?``, ``#`` or ``@``
    in it is encoded, so it cannot reach another resource, query or host. A value that
    is a whole path step (``.`` or ``..``) is refused, because it would move the
    request up the path even encoded, and so is an empty one, which would drop its
    piece (a DELETE to the collection). Raises ``PlaceholderError``.
    """

    def piece(match: re.Match[str]) -> str:
        resolved = _resolve(match.group(1), values)
        text = resolved if isinstance(resolved, str) else json.dumps(resolved, ensure_ascii=False)
        if text in (".", ".."):
            raise PlaceholderError(
                f"{{{{{match.group(1)}}}}} is {text!r}, a step of the URL's path, not a value"
            )
        if text == "":
            # `/conversations/{{session.id}}` with "" is the collection, not one of it.
            raise PlaceholderError(
                f"{{{{{match.group(1)}}}}} is empty, which would leave its piece out of the URL"
            )
        return quote(text, safe="")

    return PLACEHOLDER.sub(piece, template)


def _resolve(name: str, values: Values) -> Any:
    if name == "message":
        if values.message is None:
            raise PlaceholderError("{{message}}: there is no message here")
        return values.message
    if name == "test.id":
        return values.test_id
    if name == "test.name":
        return values.test_name
    if name == "test.history":
        return [dict(message) for message in values.history]
    if _kind(name) == "test.args":
        try:
            return get_path(dict(values.args), name.removeprefix("test.args."))
        except PathError as e:
            raise PlaceholderError(f"{{{{{name}}}}}: {e}") from None
    if _kind(name) == "run":
        return _run_value(name.removeprefix("run."), values)
    path = name.removeprefix("session.")
    try:
        value = get_path(dict(values.session), path)
    except PathError as e:
        raise PlaceholderError(f"{{{{session.{path}}}}}: {e}") from None
    if value is None:
        # As in a trigger: most likely the setup gave no value, and sending null (or
        # the text "null") would fail the agent for the setup's fault.
        raise PlaceholderError(
            f"{{{{session.{path}}}}}: the value is null: the session setup returned no value for it"
        )
    return value


def _run_value(path: str, values: Values) -> Any:
    try:
        value = get_path(dict(values.run), path)
    except PathError as e:
        raise PlaceholderError(f"{{{{run.{path}}}}}: {e}") from None
    if value is None:
        raise PlaceholderError(
            f"{{{{run.{path}}}}}: the value is null: agent.run.setup returned no value for it"
        )
    return value
