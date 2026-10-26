"""Values that came from ``${VAR}`` in the spec never reach the output (D5).

Tokens go in headers and API keys in URLs, and highbar's output ends up in CI logs. A
message that would carry one shows the variable instead: ``Bearer ${AGENT_TOKEN}``
is as useful to read, and safe to print.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote, quote_plus

# Shorter values (a port, a flag) are rarely secret, and masking them would turn every
# "8000" in every message into ${PORT}.
MIN_LENGTH = 6


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """``text`` with each secret value, in each form it is printed in, as ``${NAME}``.

    The forms: as is, stripped (a value read with its newline), URL-quoted, and
    escaped the way ``repr`` shows it.
    """
    forms: dict[str, str] = {}
    for value, name in secrets.items():
        for form in (value, value.strip(), quote(value, safe=""), quote_plus(value),
                     repr(value)[1:-1]):  # fmt: skip
            if len(form) >= MIN_LENGTH:
                forms.setdefault(form, name)
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, f"${{{forms[form]}}}")
    return text
