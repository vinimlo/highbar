"""Tests for the CLI commands that don't need a running agent."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gauntlet.cli.main import _discover_specs, app

runner = CliRunner()

SPEC = """\
agent:
  start: "true"
  endpoint: "http://localhost:8000/chat"
suites:
  - name: Menu
    tests:
      - trigger: "me mostra o [cardápio]"
        variations: 2
        expect:
          function_calls:
            - name: list_categories
"""


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with one spec under specs/, used as the cwd."""
    specs = tmp_path / "specs" / "menu"
    specs.mkdir(parents=True)
    (specs / "menu.gauntlet.yaml").write_text(textwrap.dedent(SPEC), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GAUNTLET_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


class TestDiscoverSpecs:
    def test_a_file_is_returned_as_is(self, project: Path) -> None:
        assert _discover_specs("specs/menu/menu.gauntlet.yaml") == ["specs/menu/menu.gauntlet.yaml"]

    def test_a_directory_is_searched(self, project: Path) -> None:
        assert _discover_specs("specs/") == ["specs/menu/menu.gauntlet.yaml"]

    def test_no_argument_searches_specs(self, project: Path) -> None:
        assert _discover_specs("") == ["specs/menu/menu.gauntlet.yaml"]


class TestVariationsCommand:
    @pytest.mark.parametrize("argument", [["specs/"], [], ["specs/menu/menu.gauntlet.yaml"]])
    def test_generates_and_writes_the_cache(self, project: Path, argument: list[str]) -> None:
        """`gauntlet variations specs/` is the command the README documents."""
        reply = '["qual é o cardápio?", "tem [menu] aí?"]'

        with patch("httpx.post", return_value=_Response(reply)):
            result = runner.invoke(app, ["variations", *argument])

        assert result.exit_code == 0, result.output
        # Square brackets in user text survive Rich's markup.
        assert "me mostra o [cardápio]" in result.output
        assert "tem [menu] aí?" in result.output
        assert "1 generated | 0 already cached" in result.output

        cache_file = project / ".gauntlet" / "variations.json"
        (entry,) = json.loads(cache_file.read_text(encoding="utf-8")).values()
        assert entry["variations"] == ["qual é o cardápio?", "tem [menu] aí?"]

    def test_second_run_reuses_the_cache(self, project: Path) -> None:
        with patch("httpx.post", return_value=_Response('["a", "b"]')):
            runner.invoke(app, ["variations"])
        with patch("httpx.post") as post:
            result = runner.invoke(app, ["variations"])

        assert result.exit_code == 0, result.output
        assert "0 generated | 1 already cached" in result.output
        post.assert_not_called()

    def test_fewer_than_requested_is_reported(self, project: Path) -> None:
        """Duplicates collapse to one entry, and the report has to say so."""
        with patch("httpx.post", return_value=_Response('["a", "A", "a"]')):
            result = runner.invoke(app, ["variations"])

        assert result.exit_code == 0, result.output
        assert "1 of 2 requested" in result.output
        assert "1 with fewer than requested" in result.output

        with patch("httpx.post") as post:
            again = runner.invoke(app, ["variations"])

        assert "cached with 1 of 2" in again.output
        post.assert_not_called()

    def test_broken_cache_file_is_a_clean_error(self, project: Path) -> None:
        cache_file = project / ".gauntlet" / "variations.json"
        cache_file.parent.mkdir()
        cache_file.write_text("<<<<<<< HEAD\n{}\n")

        result = runner.invoke(app, ["variations"])

        assert result.exit_code == 1
        assert "not valid JSON" in result.output
        assert "Traceback" not in result.output

    def test_missing_api_key_is_a_clean_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GAUNTLET_LLM_API_KEY")

        result = runner.invoke(app, ["variations"])

        assert result.exit_code == 1
        assert "GAUNTLET_LLM_API_KEY" in result.output
