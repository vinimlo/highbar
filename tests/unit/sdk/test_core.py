"""Tests for Gauntlet class instantiation and configuration."""

from pathlib import Path
from unittest.mock import patch

from gauntlet.sdk.core import Gauntlet


class TestGauntletClass:
    def test_default_no_traces(self) -> None:
        gt = Gauntlet()
        assert gt.traces is None
        assert gt.enabled is False

    def test_explicit_traces_path(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))
        assert gt.traces == str(trace_file)
        assert gt.enabled is True

    def test_reads_env_var(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        with patch.dict("os.environ", {"GAUNTLET_TRACES_URL": str(trace_file)}):
            gt = Gauntlet()
            assert gt.traces == str(trace_file)
            assert gt.enabled is True

    def test_explicit_overrides_env(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"GAUNTLET_TRACES_URL": "/env/path"}):
            gt = Gauntlet(traces="/explicit/path")
            assert gt.traces == "/explicit/path"

    def test_tool_registry(self) -> None:
        gt = Gauntlet()

        @gt.tool
        def fn_a() -> None:
            pass

        @gt.tool
        def fn_b() -> None:
            pass

        assert "fn_a" in gt.tools
        assert "fn_b" in gt.tools
