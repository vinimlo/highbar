"""Dotted paths into JSON values: ``data.reply``, ``choices.0.message.content``."""

from __future__ import annotations

from typing import Any


class PathError(ValueError):
    """A path step found nothing; the message says what was there instead."""


def get_path(data: Any, path: str) -> Any:
    """The value at ``path``: keys separated by dots, numeric steps index lists."""
    current = data
    for step in path.split("."):
        if isinstance(current, dict):
            if step not in current:
                keys = ", ".join(str(key) for key in current) or "none"
                raise PathError(f"no key '{step}' (keys: {keys})")
            current = current[step]
        elif isinstance(current, list):
            if not step.isdigit():
                raise PathError(f"'{step}' is not an index into a list of {len(current)}")
            index = int(step)
            if index >= len(current):
                raise PathError(f"index {index} is out of range (a list of {len(current)})")
            current = current[index]
        else:
            raise PathError(f"cannot read '{step}' from a {type(current).__name__}")
    return current
