"""Tests for Highbar class instantiation and configuration."""

from pathlib import Path
from unittest.mock import patch

from highbar.sdk.core import Highbar


class TestHighbarClass:
    def test_default_no_traces(self) -> None:
        hb = Highbar()
        assert hb.traces is None
        assert hb.enabled is False

    def test_explicit_traces_path(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        hb = Highbar(traces=str(trace_file))
        assert hb.traces == str(trace_file)
        assert hb.enabled is True

    def test_reads_env_var(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        with patch.dict("os.environ", {"HIGHBAR_TRACES_URL": str(trace_file)}):
            hb = Highbar()
            assert hb.traces == str(trace_file)
            assert hb.enabled is True

    def test_explicit_overrides_env(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"HIGHBAR_TRACES_URL": "/env/path"}):
            hb = Highbar(traces="/explicit/path")
            assert hb.traces == "/explicit/path"

    def test_tool_registry(self) -> None:
        hb = Highbar()

        @hb.tool
        def fn_a() -> None:
            pass

        @hb.tool
        def fn_b() -> None:
            pass

        assert "fn_a" in hb.tools
        assert "fn_b" in hb.tools
