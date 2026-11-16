"""Template engine — resolves {{previous.*}} expressions in triggers.

``previous`` is what the earlier turns of the execution left: ``function_calls``, the
tool calls of all of them, ``turns``, each one's ``response``, and ``response``, the
reply of the turn right before (HB-20). ``{{session.<path>}}`` is a value of the
object the session setup returned, as in the request.

Both are resolved in one pass over what the spec wrote: a reply or a session value
that holds ``{{...}}`` goes in as text, and is never read as a template again.

Nothing that is not a value goes out looking like one: a path that ends on ``null``,
or ``{{session}}`` and ``{{previous}}`` with no path, are errors, not the text
``null`` or the braces as written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

from highbar._narrow import as_dict, as_list
from highbar.domain.models import Trace
from highbar.engine.paths import PathError, get_path

# Spaces inside the braces are allowed: `{{ previous.x }}` is how most template
# languages are written, and rejecting it would read as "no such value". A path holds
# no brace, so a template ends at its own `}}`. No path at all is matched too, to be
# refused: `{{session}}` would otherwise go to the agent as it was written.
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*(previous|session)(?:\.([^{}]*?))?\s*\}\}")

_TRACE_FIELDS = sorted(f.name for f in fields(Trace))


def session_placeholders(text: str) -> list[str]:
    """The ``{{session.<path>}}`` in ``text``, written without spaces, in order."""
    return [
        f"{{{{session.{match.group(2)}}}}}"
        for match in _TEMPLATE_PATTERN.finditer(text)
        if match.group(1) == "session" and match.group(2)
    ]


def pathless_templates(text: str) -> list[str]:
    """The ``{{session}}`` and ``{{previous}}`` in ``text`` that name no value."""
    return [
        f"{{{{{match.group(1)}}}}}"
        for match in _TEMPLATE_PATTERN.finditer(text)
        if not match.group(2)
    ]


def no_path_message(kind: str) -> str:
    """Why ``{{<kind>}}`` alone is refused, and what to write instead."""
    example = f"{{{{{kind}.<key>}}}}" if kind in ("session", "run") else "{{previous.response}}"
    return f"names no value: write a path, as {example}"


class TemplateError(ValueError):
    """A template that names a value that is not there.

    The message is ``{{previous.<path>}}: <reason>``, and the reason says what *was*
    there, so the spec can be fixed without rerunning to find out.
    """


@dataclass(frozen=True)
class _Earlier:
    """An earlier turn of the execution, as a template sees it."""

    number: int  # from 1, as "turn 2/3" counts them
    response: str | None  # None when its reply was not read


def resolve_template(
    template: str,
    traces: list[Trace],
    responses: Sequence[str | None] = (),
    session: Mapping[str, Any] | None = None,
) -> str:
    """Replace all {{previous.*}} and {{session.*}} expressions with actual values.

    ``traces`` are the tool calls of the earlier turns, ``responses`` their replies, in
    order, and ``session`` what the session setup returned. A string goes in as is;
    anything else (number, list, dict) as JSON, which is what a person would type to
    the agent, not a Python repr.
    """
    earlier = [_Earlier(number, reply) for number, reply in enumerate(responses, start=1)]

    def replacer(match: re.Match[str]) -> str:
        kind, path = match.group(1), match.group(2)
        if not path:
            raise TemplateError(f"{{{{{kind}}}}}: {no_path_message(kind)}")
        try:
            if kind == "session":
                value = _session_value(path, session)
            else:
                value = _resolve_path(path, traces, earlier)
        except _Missing as e:
            raise TemplateError(f"{{{{{kind}.{path}}}}}: {e}") from None
        if value is None:
            # Most likely the value was never produced: sent as "null", it would fail
            # the agent for a message nobody meant to write.
            cause = (
                "the session setup returned no value for it"
                if kind == "session"
                else "the earlier turn left no value there"
            )
            raise TemplateError(f"{{{{{kind}.{path}}}}}: the value is null: {cause}")
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    return _TEMPLATE_PATTERN.sub(replacer, template)


class _Missing(Exception):
    """A step of the path found nothing; the message says what was available."""


def _session_value(path: str, session: Mapping[str, Any] | None) -> Any:
    if session is None:
        raise _Missing("there is no session: agent.session's setup returns it")
    try:
        return get_path(dict(session), path)
    except PathError as e:
        raise _Missing(str(e)) from None


def _resolve_path(path: str, traces: list[Trace], earlier: list[_Earlier]) -> Any:
    """Resolve a dotted path like 'function_calls[0].result.order_id'."""
    tokens = _tokenize_path(path)
    current: Any = {"function_calls": traces, "response": None, "turns": earlier}
    name = "function_calls"
    if tokens[0] == "response":
        # The turn right before: `previous.response` is `previous.turns[-1].response`.
        if not earlier:
            raise _Missing("no earlier turn: the first message has no previous response")
        current = earlier[-1]

    for part in tokens:
        if isinstance(part, int):
            counted = "calls in earlier turns" if current is traces else None
            if current is earlier:
                counted = "earlier turns"
            current = _index(current, part, name, counted=counted)
            continue
        name = part
        if isinstance(current, _Earlier):
            if part != "response":
                raise _Missing(f"no field '{part}' (fields: response)")
            if current.response is None:
                raise _Missing(f"turn {current.number} has no response: its reply was not read")
            current = current.response
        elif isinstance(current, Trace):
            if part not in _TRACE_FIELDS:
                raise _Missing(f"no field '{part}' (fields: {', '.join(_TRACE_FIELDS)})")
            current = getattr(current, part)
        elif (mapping := as_dict(current)) is not None:
            if part not in mapping:
                keys = ", ".join(str(key) for key in mapping) or "none"
                raise _Missing(f"no key '{part}' (keys: {keys})")
            current = mapping[part]
        else:
            raise _Missing(f"cannot read '{part}' from a {type(current).__name__}")

    if isinstance(current, _Earlier) or current is earlier:
        # A turn as JSON would carry an unread reply out as "response": null, the very
        # message the rule above refuses to send.
        raise _Missing("a turn is not a value: end the path with .response")
    return current


def _index(current: Any, index: int, name: str, *, counted: str | None) -> Any:
    items = as_list(current)
    if items is None:
        raise _Missing(f"cannot index '{name}[{index}]' into a {type(current).__name__}")
    if index >= len(items):
        if counted is not None:
            raise _Missing(f"only {len(items)} {counted}")
        raise _Missing(f"only {len(items)} items in '{name}'")
    return items[index]


_SEGMENT = re.compile(r"(\w+)((?:\[\d+\])*)")


def _tokenize_path(path: str) -> list[str | int]:
    """Split 'function_calls[0].result.items[1][0]' into tokens.

    A segment that is not a name followed by zero or more [index] is an error: reading
    past the junk used to resolve `m[1]x` as `m[1]`, a wrong value without a word.
    """
    tokens: list[str | int] = []
    for segment in path.split("."):
        match = _SEGMENT.fullmatch(segment)
        if match is None:
            raise _Missing(f"invalid path segment '{segment}'")
        tokens.append(match.group(1))
        tokens.extend(int(index) for index in re.findall(r"\[(\d+)\]", match.group(2)))
    return tokens
