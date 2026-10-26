"""Dotted paths into JSON values: ``data.reply``, ``choices.0.message.content``."""

from __future__ import annotations

from typing import Any

from highbar._narrow import as_dict, as_list


class PathError(ValueError):
    """A path step found nothing; the message says what was there instead."""


def get_path(data: Any, path: str) -> Any:
    """The value at ``path``: keys separated by dots, numeric steps index lists."""
    current = data
    for step in path.split("."):
        if (mapping := as_dict(current)) is not None:
            if step not in mapping:
                keys = ", ".join(str(key) for key in mapping) or "none"
                raise PathError(f"no key '{step}' (keys: {keys})")
            current = mapping[step]
        elif (items := as_list(current)) is not None:
            if not step.isdigit():
                raise PathError(f"'{step}' is not an index into a list of {len(items)}")
            index = int(step)
            if index >= len(items):
                raise PathError(f"index {index} is out of range (a list of {len(items)})")
            current = items[index]
        else:
            raise PathError(f"cannot read '{step}' from a {type(current).__name__}")
    return current
