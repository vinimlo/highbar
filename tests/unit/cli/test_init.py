"""`highbar init` writes the quickstart: an agent and a spec that run green."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from highbar.cli.main import app

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "quickstart"
WRITTEN = ["agent.py", "specs/quickstart.highbar.yaml"]


def _init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    return result.output


def test_init_writes_the_quickstart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = _init(tmp_path, monkeypatch)

    for name in WRITTEN:
        assert (tmp_path / name).read_bytes() == (EXAMPLE / name).read_bytes(), name
    assert "highbar run" in output


def test_init_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "agent.py").write_text("mine\n", encoding="utf-8")

    output = _init(tmp_path, monkeypatch)

    assert (tmp_path / "agent.py").read_text(encoding="utf-8") == "mine\n"
    assert "Skipped agent.py" in output
    assert (tmp_path / "specs" / "quickstart.highbar.yaml").exists()
