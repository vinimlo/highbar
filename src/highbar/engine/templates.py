"""Template engine — resolves {{previous.*}} expressions in triggers."""

from __future__ import annotations

import json
import re
from dataclasses import fields
from typing import Any

from highbar.domain.models import Trace

# Spaces inside the braces are allowed: `{{ previous.x }}` is how most template
# languages are written, and rejecting it would read as "no such value".
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*previous\.(.+?)\s*\}\}")

_TRACE_FIELDS = sorted(f.name for f in fields(Trace))


class TemplateError(ValueError):
    """A template that names a value that is not there.

    The message is ``{{previous.<path>}}: <reason>``, and the reason says what *was*
    there, so the spec can be fixed without rerunning to find out.
    """


def resolve_template(template: str, traces: list[Trace]) -> str:
    """Replace all {{previous.*}} expressions with actual values from traces.

    A string goes in as is; anything else (number, list, dict) as JSON, which is what
    a person would type to the agent, not a Python repr.
    """

    def replacer(match: re.Match[str]) -> str:
        path = match.group(1)
        try:
            value = _resolve_path(path, traces)
        except _Missing as e:
            raise TemplateError(f"{{{{previous.{path}}}}}: {e}") from None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    return _TEMPLATE_PATTERN.sub(replacer, template)


class _Missing(Exception):
    """A step of the path found nothing; the message says what was available."""


def _resolve_path(path: str, traces: list[Trace]) -> Any:
    """Resolve a dotted path like 'function_calls[0].result.order_id'."""
    current: Any = {"function_calls": traces}
    name = "function_calls"

    for part in _tokenize_path(path):
        if isinstance(part, int):
            current = _index(current, part, name, top_level=current is traces)
            continue
        name = part
        if isinstance(current, Trace):
            if part not in _TRACE_FIELDS:
                raise _Missing(f"no field '{part}' (fields: {', '.join(_TRACE_FIELDS)})")
            current = getattr(current, part)
        elif isinstance(current, dict):
            if part not in current:
                keys = ", ".join(str(key) for key in current) or "none"
                raise _Missing(f"no key '{part}' (keys: {keys})")
            current = current[part]
        else:
            raise _Missing(f"cannot read '{part}' from a {type(current).__name__}")

    return current


def _index(current: Any, index: int, name: str, *, top_level: bool) -> Any:
    if not isinstance(current, list):
        raise _Missing(f"cannot index '{name}[{index}]' into a {type(current).__name__}")
    if index >= len(current):
        if top_level:
            raise _Missing(f"only {len(current)} calls in earlier turns")
        raise _Missing(f"only {len(current)} items in '{name}'")
    return current[index]


def _tokenize_path(path: str) -> list[str | int]:
    """Split 'function_calls[0].result.order_id' into tokens."""
    tokens: list[str | int] = []
    for segment in path.split("."):
        bracket_match = re.match(r"(\w+)\[(\d+)\]", segment)
        if bracket_match:
            tokens.append(bracket_match.group(1))
            tokens.append(int(bracket_match.group(2)))
        else:
            tokens.append(segment)
    return tokens
