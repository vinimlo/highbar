"""Values that came from ``${VAR}`` in the spec never reach the output (D5).

Tokens go in headers and API keys in URLs, and highbar's output ends up in CI logs. A
message that would carry one shows the variable instead: ``Bearer ${AGENT_TOKEN}``
is as useful to read, and safe to print.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote, quote_plus

# Shorter values (a port, a flag) are rarely secret, and masking them would turn every
# "8000" in every message into ${PORT}.
MIN_LENGTH = 6

# What ends a message cut at its limit (``truncate``).
ELLIPSIS = "…"
# A cut value is told by this many characters: with fewer, every message that happens
# to stop at a letter some value starts with would show a mask for nothing.
MIN_CUT = 3


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """``text`` with each secret value, in each form it is printed in, as ``${NAME}``.

    The forms: as is, stripped (a value read with its newline), URL-quoted, and
    escaped the way ``repr`` shows it. A message is cut before it gets here, so a value
    that the cut split is matched by its start, against the ellipsis.
    """
    forms: dict[str, str] = {}
    for value, name in secrets.items():
        for form in (value, value.strip(), quote(value, safe=""), quote_plus(value),
                     repr(value)[1:-1]):  # fmt: skip
            if len(form) >= MIN_LENGTH:
                forms.setdefault(form, name)
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, f"${{{forms[form]}}}")
    if ELLIPSIS in text:
        starts = {form[:n]: name for form, name in forms.items() for n in range(MIN_CUT, len(form))}
        for start in sorted(starts, key=len, reverse=True):
            text = text.replace(start + ELLIPSIS, f"${{{starts[start]}}}{ELLIPSIS}")
    return text


def redact_value(value: Any, secrets: Mapping[str, str]) -> Any:
    """``value`` with ``redact`` applied all the way down: strings, keys, numbers.

    A number that holds a secret becomes the masked text: a PIN in a tool's arguments
    is as secret as a token.
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        return {redact_value(k, secrets): redact_value(v, secrets) for k, v in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[Any] | tuple[Any, ...]", value)
        return type(items)(redact_value(item, secrets) for item in items)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
        masked = redact(text, secrets)
        return value if masked == text else masked
    return value
