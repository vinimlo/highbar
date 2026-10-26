"""The release workflow publishes only a tag that is the package's version."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check", ROOT / "scripts" / "check_release_tag.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pyproject(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "highbar"\nversion = "{version}"\n', encoding="utf-8")
    return path


def test_the_version_tag_passes(tmp_path: Path) -> None:
    assert _script().main(["v0.1.0rc1"], _pyproject(tmp_path, "0.1.0rc1")) == 0


def test_another_tag_fails_and_says_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _script().main(["v0.1.0"], _pyproject(tmp_path, "0.1.0rc1")) == 1
    err = capsys.readouterr().err
    assert "v0.1.0 " in err
    assert "0.1.0rc1" in err


def test_the_repository_version_has_a_tag_form() -> None:
    assert (
        _script().main([f"v{_script().version(ROOT / 'pyproject.toml')}"], ROOT / "pyproject.toml")
        == 0
    )
