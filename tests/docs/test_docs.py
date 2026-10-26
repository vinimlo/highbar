"""The docs only show what runs.

Every YAML block in the README and the site is a spec that passes `highbar validate`,
a declared fragment of one (validated inside a minimal spec), or declared as some
other YAML. Every Python block compiles. The quick start runs, command by command,
and the output the docs show is the output it prints.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec
from tests.docs.blocks import Block, blocks, spec_for

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [
    ROOT / "README.md",
    *sorted(p for p in (ROOT / "site").rglob("*.md") if "node_modules" not in p.parts),
]

ALL_BLOCKS = [block for source in SOURCES for block in blocks(source)]
YAML = [b for b in ALL_BLOCKS if b.lang in ("yaml", "yml")]
PYTHON = [b for b in ALL_BLOCKS if b.lang == "python"]
JSON = [b for b in ALL_BLOCKS if b.lang in ("json", "jsonl")]
QUICKSTARTS = [b for b in ALL_BLOCKS if b.marker == "quickstart"]
OUTPUTS = [b for b in ALL_BLOCKS if b.marker.startswith("output:")]

# The scenarios an output block can name, and the edit that sets each one up.
SCENARIOS: dict[str, tuple[str, str] | None] = {
    "quickstart": None,
    "quickstart-broken": ("GUARDRAILS = True", "GUARDRAILS = False"),
}


def _ids(items: list[Block]) -> list[str]:
    return [b.where for b in items]


def test_every_yaml_block_is_checked() -> None:
    """A block that is neither a spec nor declared would be skipped in silence."""
    refused: list[str] = []
    for block in YAML:
        try:
            spec_for(block)
        except ValueError as e:
            refused.append(str(e))
    assert not refused, refused


@pytest.mark.parametrize("block", YAML, ids=_ids(YAML))
def test_yaml_block_is_valid(block: Block, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    try:
        text = spec_for(block)
    except ValueError:
        return  # reported by test_every_yaml_block_is_checked
    if text is None:
        return
    # ${VAR} in a doc stands for the reader's own secret.
    for name in re.findall(r"\$\{(\w+)\}", text):
        monkeypatch.setenv(name, f"docs-test-{name.lower()}-value")
    path = tmp_path / "block.highbar.yaml"
    path.write_text(text, encoding="utf-8")
    try:
        parse_spec(str(path))
    except Exception as e:  # the block's location is what the reader needs
        pytest.fail(f"{block.where}: {e}")


@pytest.mark.parametrize("block", PYTHON, ids=_ids(PYTHON))
def test_python_block_compiles(block: Block) -> None:
    compile(block.text, block.where, "exec")


@pytest.mark.parametrize("block", JSON, ids=_ids(JSON))
def test_json_block_parses(block: Block) -> None:
    """A JSON block is one document, or JSON Lines like a trace file."""
    try:
        json.loads(block.text)
    except json.JSONDecodeError:
        for line in filter(None, (line.strip() for line in block.text.splitlines())):
            json.loads(line)


def _highbar(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", *args],
        cwd=cwd,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=180,
    )


def _commands(block: Block) -> list[list[str]]:
    lines = [line.strip().removeprefix("$ ") for line in block.text.splitlines()]
    return [shlex.split(line) for line in lines if line and not line.startswith("#")]


def _run_quickstart(block: Block, scenario: str) -> str:
    """Run the block's commands in an empty directory; the last one's output.

    ``pip install`` is skipped: the tests run with this checkout installed, which is
    the package the command would install. The README and the site show the same
    commands, so each scenario runs once.
    """
    commands = [c for c in _commands(block) if c[:2] != ["pip", "install"]]
    assert commands and all(c[0] == "highbar" for c in commands), (
        f"{block.where}: the quick start may only run pip install and highbar: {commands}"
    )
    return _run_commands(tuple(tuple(c) for c in commands), scenario)


@functools.cache
def _run_commands(commands: tuple[tuple[str, ...], ...], scenario: str) -> str:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        result = None
        for index, command in enumerate(commands):
            last = index == len(commands) - 1
            if last and (edit := SCENARIOS[scenario]) is not None:
                agent = cwd / "agent.py"
                agent.write_text(agent.read_text(encoding="utf-8").replace(*edit), encoding="utf-8")
            result = _highbar(list(command[1:]), cwd)
            expected = 1 if last and scenario != "quickstart" else 0
            assert result.returncode == expected, (
                f"`{shlex.join(command)}` ({scenario}) exited {result.returncode}\n"
                + result.stdout
                + result.stderr
            )
        assert result is not None
        return result.stdout


@pytest.mark.timeout(180)
@pytest.mark.parametrize("block", QUICKSTARTS, ids=_ids(QUICKSTARTS))
def test_quickstart_runs(block: Block) -> None:
    _run_quickstart(block, "quickstart")


def _normalized(text: str) -> str:
    text = re.sub(r"\d+ms\b", "<ms>", text)
    text = re.sub(r"\d+\.\d+s\b", "<s>", text)
    text = re.sub(r"Highbar v\S+", "Highbar v<version>", text)
    text = re.sub(r"-\d{8}-\d{6}(-\d+)?\.log", "-<time>.log", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip("\n")


@pytest.mark.timeout(180)
@pytest.mark.parametrize("block", OUTPUTS, ids=_ids(OUTPUTS))
def test_output_shown_is_the_output_printed(block: Block) -> None:
    scenario = block.marker.removeprefix("output:").strip()
    assert scenario in SCENARIOS, f"{block.where}: unknown output scenario {scenario!r}"
    quickstart = [b for b in QUICKSTARTS if b.path == block.path]
    assert quickstart, f"{block.where}: no <!-- quickstart --> block in {block.path.name}"

    printed = _run_quickstart(quickstart[0], scenario)

    assert _normalized(block.text) == _normalized(printed)


FILES = [b for b in ALL_BLOCKS if b.marker.startswith("file:")]


@pytest.mark.parametrize("block", FILES, ids=_ids(FILES))
def test_file_shown_is_the_file(block: Block) -> None:
    """A block marked <!-- file: path --> is that file, as it is in the repository."""
    path = ROOT / block.marker.removeprefix("file:").strip()
    assert block.text == path.read_text(encoding="utf-8"), f"{block.where} differs from {path}"


def test_the_readme_has_a_quick_start_and_its_output() -> None:
    readme = [b for b in ALL_BLOCKS if b.path.name == "README.md"]
    assert [b.marker for b in readme if b.marker == "quickstart"] == ["quickstart"]
    assert "output: quickstart" in [b.marker for b in readme]


def test_site_prose_has_no_bare_placeholders() -> None:
    """VitePress compiles each page as a Vue template: a bare {{x}} in prose breaks the build.

    Code fences are left alone by VitePress; prose needs <code v-pre> or ::: v-pre.
    """
    found: list[str] = []
    for page in SOURCES:
        if page.name == "README.md":
            continue
        in_fence = in_vpre = False
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
            elif stripped == "::: v-pre":
                in_vpre = True
            elif stripped == ":::" and in_vpre:
                in_vpre = False
            elif not (in_fence or in_vpre):
                raw_code = re.findall(r"<code v-pre>(.*?)</code>", line)
                # Inside raw HTML, <path> is a tag to Vue: it must be &lt;path&gt;.
                if "{{" in re.sub(r"<code v-pre>.*?</code>", "", line) or any(
                    "<" in code or ">" in code for code in raw_code
                ):
                    found.append(f"{page.relative_to(ROOT)}:{number}")
    assert not found, f"wrap these in <code v-pre> (with &lt; &gt;) or ::: v-pre: {found}"
