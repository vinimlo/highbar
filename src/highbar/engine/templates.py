"""Template engine — resolves {{previous.*}} expressions in triggers."""

from __future__ import annotations

import re
from typing import Any

from highbar.domain.models import Trace

_TEMPLATE_PATTERN = re.compile(r"\{\{previous\.(.+?)\}\}")


def resolve_template(template: str, traces: list[Trace]) -> str:
    """Replace all {{previous.*}} expressions with actual values from traces."""

    def replacer(match: re.Match[str]) -> str:
        path = match.group(1)
        value = _resolve_path(path, traces)
        return str(value)

    return _TEMPLATE_PATTERN.sub(replacer, template)


def _resolve_path(path: str, traces: list[Trace]) -> Any:
    """Resolve a dotted path like 'function_calls[0].result.order_id'."""
    parts = _tokenize_path(path)
    current: Any = {"function_calls": traces}

    for part in parts:
        if isinstance(part, int):
            current = current[part]
        elif isinstance(current, Trace):
            current = getattr(current, part)
        elif isinstance(current, dict):
            current = current[part]
        else:
            current = getattr(current, part)

    return current


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
