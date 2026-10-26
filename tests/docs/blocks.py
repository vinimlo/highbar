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
from typing import Any

import yaml

_FENCE = re.compile(r"^(?P<indent> *)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_MARKER = re.compile(r"^\s*<!--\s*(?P<marker>.*?)\s*-->\s*$")
# A blockquote's "> " prefix, so a fence inside a note is found like any other.
_QUOTE = re.compile(r"^\s*(?:>\s?)+")

# What a fragment is wrapped in to become a spec that can be validated.
_AGENT = {"start": "python agent.py", "endpoint": "http://127.0.0.1:8731/chat"}
_TEST = {"trigger": "hi", "expect": {"response": {"contains": "hi"}}}
# YAML that is not a spec, declared as such in the fence: ```yaml workflow
OTHER_YAML_ROLES = frozenset({"workflow"})


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
    lines = [_QUOTE.sub("", line) for line in path.read_text(encoding="utf-8").splitlines()]
    found: list[Block] = []
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        fence, indent = opening.group("fence"), len(opening.group("indent"))
        words = opening.group("info").split()
        # `yaml{3}`, `YAML`, `yaml [title]`: the language is the word before any attribute.
        lang = re.split(r"[{\[]", words[0])[0].lower() if words else ""
        role = words[1] if len(words) > 1 and words[1][0] not in "{[" else ""
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
                lang=lang,
                role=role,
                marker=marker.group("marker") if marker else "",
                text="\n".join(body) + "\n",
            )
        )
        index = end + 1
    return found


def spec_for(block: Block) -> str | None:
    """The spec a YAML block stands for, to validate; ``None`` for declared other YAML.

    Raises ``ValueError`` for a block that is neither: an undeclared fragment, an
    ``agent`` block with more than the agent, a ``workflow`` block holding a spec.
    """
    data: Any = yaml.safe_load(block.text)
    keys = set(data) if isinstance(data, dict) else set()
    if block.role == "":
        if "suites" not in keys:
            raise ValueError(
                f"{block.where}: a yaml block that is not a whole spec must declare its "
                "role in the fence (```yaml test, ```yaml agent, ```yaml workflow)"
            )
        return block.text
    if block.role == "test":
        tests = data if isinstance(data, list) else [data]
        return yaml.safe_dump({"agent": _AGENT, "suites": [{"name": "S", "tests": tests}]})
    if block.role == "agent":
        if keys != {"agent"}:
            raise ValueError(f"{block.where}: a ```yaml agent block holds only the agent: key")
        return yaml.safe_dump({**data, "suites": [{"name": "S", "tests": [_TEST]}]})
    if block.role in OTHER_YAML_ROLES:
        if keys & {"agent", "suites"}:
            raise ValueError(f"{block.where}: a ```yaml {block.role} block cannot hold a spec")
        return None
    raise ValueError(f"{block.where}: unknown role {block.role!r} for a yaml block")
