"""The README's translations say what they translate, and stay in step with it.

Each README.<lang>.md opens with the line scripts/readme_source.py prints, which names
the English README it was translated from by a hash of its text. When README.md changes,
the hash no longer matches, and the test names each translation to bring up to date.
The code blocks of a translation are the English ones, byte for byte, so a command or an
output cannot drift. test_docs.py runs and validates them in every language.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.readme_source import readme_hash

from tests.docs.blocks import blocks

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("pt-BR", "es", "zh-CN", "ja", "ko", "fr", "de")
TRANSLATIONS = [ROOT / f"README.{language}.md" for language in LANGUAGES]
READMES = [ROOT / "README.md", *TRANSLATIONS]

_SOURCE = re.compile(r"^<!-- source: README\.md sha256=(?P<hash>[0-9a-f]{12}) commit=\S+ -->$")


def test_every_language_has_its_translation_and_no_other() -> None:
    found = sorted(p.name for p in ROOT.glob("README.*.md"))
    assert found == sorted(p.name for p in TRANSLATIONS)


@pytest.mark.parametrize("path", TRANSLATIONS, ids=LANGUAGES)
def test_the_translation_is_of_the_readme_as_it_is(path: Path) -> None:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    source = _SOURCE.match(first)
    assert source, f"{path.name} must open with the line `python scripts/readme_source.py` prints"
    assert source.group("hash") == readme_hash(ROOT), (
        f"{path.name} translates an older README.md ({first}). Bring it up to date with "
        "README.md, then replace its first line with `python scripts/readme_source.py`."
    )


def _code(path: Path) -> list[tuple[str, str, str, str]]:
    return [(b.lang, b.role, b.marker, b.text) for b in blocks(path)]


@pytest.mark.parametrize("path", TRANSLATIONS, ids=LANGUAGES)
def test_the_code_blocks_are_the_english_ones(path: Path) -> None:
    assert _code(path) == _code(ROOT / "README.md")


def _structure(path: Path) -> list[str]:
    """The shape of the text outside code, which a translation keeps.

    Headings with their level, list items, table rows and the target of every link and
    image, in order. The note a translation adds and the links between the READMEs are
    left out, since they differ by language. A hash alone would pass a translation that
    lost a section or a bullet once its first line was replaced (review of #105).
    """
    shape: list[str] = []
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith(">"):
            continue
        if heading := re.match(r"^(#{1,6}) ", line):
            shape.append(f"h{len(heading.group(1))}")
        elif re.match(r"^(?:[-*]|\d+\.) ", line):
            shape.append("li")
        elif stripped.startswith("|"):
            shape.append("tr")
        for target in re.findall(r"\]\(([^)]*)\)|src=\"([^\"]*)\"", line):
            link = target[0] or target[1]
            if not re.fullmatch(r"README(\.[\w-]+)?\.md", link):
                shape.append(f"link {link}")
    return shape


@pytest.mark.parametrize("path", TRANSLATIONS, ids=LANGUAGES)
def test_the_structure_is_the_english_one(path: Path) -> None:
    english, translated = _structure(ROOT / "README.md"), _structure(path)
    first = next(
        (i for i, (a, b) in enumerate(zip(english, translated, strict=False)) if a != b),
        min(len(english), len(translated)),
    )
    assert translated == english, (
        f"{path.name} differs from README.md in its headings, list items, table rows or links, "
        f"first at item {first}: {english[first : first + 1]} in English, "
        f"{translated[first : first + 1]} in the translation"
    )


@pytest.mark.parametrize("path", READMES, ids=("en", *LANGUAGES))
def test_the_language_row_links_every_other_readme(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    others = [p.name for p in READMES if p != path]
    missing = [name for name in others if f"]({name})" not in text]
    assert not missing, f"{path.name} does not link {missing}"


def _prose(path: Path) -> list[tuple[int, str]]:
    """Each line outside code, with inline code, links' targets, URLs and HTML removed."""
    lines: list[tuple[int, str]] = []
    in_fence = in_html = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # An HTML tag that spans lines, like the demo's <img>.
        if in_html:
            in_html = ">" not in stripped
            continue
        if stripped.startswith("<") and ">" not in stripped:
            in_html = True
            continue
        text = re.sub(r"<!--.*?-->|<[^>]+>", "", line)
        text = re.sub(r"`[^`]*`", "", text)
        text = re.sub(r"\]\([^)]*\)", "]", text)
        text = re.sub(r"https?://\S+", "", text)
        lines.append((number, text))
    return lines


@pytest.mark.parametrize("path", READMES, ids=("en", *LANGUAGES))
def test_the_prose_has_no_colons(path: Path) -> None:
    """A colon in running text reads as a list of labels, so the README says it in sentences.

    Code, commands, URLs and the output blocks keep theirs.
    """
    found = [
        f"{path.name}:{n}: {text.strip()}" for n, text in _prose(path) if re.search("[:：]", text)
    ]
    assert not found, "\n".join(found)
