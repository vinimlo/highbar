"""What the formats of the report share: why a run is not a pass, and printable text."""

from __future__ import annotations

import re
from typing import Any

# What XML 1.0 can hold. Anything else (a NUL, an escape code, a lone surrogate the
# agent sent) is written as its Python escape, as the JSON report keeps it readable.
_NOT_PRINTABLE = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


def printable(value: Any) -> str:
    """``value`` as text, with what no reader can show written as its escape."""

    def escape(match: re.Match[str]) -> str:
        code = ord(match.group())
        return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"

    return _NOT_PRINTABLE.sub(escape, str(value))


def run_error(body: dict[str, Any], *, failed: bool) -> str | None:
    """Why the run itself is not a pass, when its tests would not say so.

    ``failed`` says whether a test failed or errored: a format that shows tests reads
    as green when none did, so a run that did not finish, was stopped, or exited with
    an error code anyway needs this said apart.
    """
    code = body["exit_code"]
    if code is None:
        return "the run did not finish: highbar stopped before writing its result"
    stopped = body["stopped"]
    if stopped is not None:
        return f"{stopped['reason']}: {stopped['message']} (exit code {code})"
    if code != 0 and not failed:
        return f"the run exited with code {code}"
    return None
