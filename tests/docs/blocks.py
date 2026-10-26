"""Fenced code blocks of a Markdown file, with what the docs declare about each.

A fence's info string is ``<lang> [<role>]``: ```` ```yaml test ```` is a YAML block in
the role of one test. GitHub and VitePress read only the first word, so the role does
not change how the block renders. An HTML comment on the line before a fence marks
it for the test to run or compare (``<!-- quickstart -->``, ``<!-- output: x -->``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FENCE = re.compile(r"^(?P<indent> *)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_MARKER = re.compile(r"^\s*<!--\s*(?P<marker>.*?)\s*-->\s*$")


@dataclass(frozen=True)
class Block:
    path: Path
    line: int  # of the opening fence, 1-based
    lang: str
    role: str
    marker: str
    text: str

    @property
    def where(self) -> str:
        return f"{self.path.name}:{self.line}"


def blocks(path: Path) -> list[Block]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[Block] = []
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        fence, indent = opening.group("fence"), len(opening.group("indent"))
        words = opening.group("info").split()
        previous = next((lines[i] for i in range(index - 1, -1, -1) if lines[i].strip()), "")
        marker = _MARKER.match(previous)
        body: list[str] = []
        end = index + 1
        while end < len(lines) and not lines[end].strip().startswith(fence):
            body.append(lines[end][indent:] if lines[end].startswith(" " * indent) else lines[end])
            end += 1
        found.append(
            Block(
                path=path,
                line=index + 1,
                lang=words[0] if words else "",
                role=words[1] if len(words) > 1 else "",
                marker=marker.group("marker") if marker else "",
                text="\n".join(body) + "\n",
            )
        )
        index = end + 1
    return found
