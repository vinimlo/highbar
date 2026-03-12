"""Tests for subprocess adapter."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gauntlet.adapters.subprocess_adapter import SubprocessAdapter


class TestSubprocessAdapter:
    def test_start_launches_process(self) -> None:
        with patch("gauntlet.adapters.subprocess_adapter.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()

            adapter = SubprocessAdapter(start_cmd="python agent.py")
            adapter.start()

            mock_popen.assert_called_once()
            args = mock_popen.call_args
            assert args[0][0] == ["python", "agent.py"]

    def test_start_injects_env(self) -> None:
        with patch("gauntlet.adapters.subprocess_adapter.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()

            adapter = SubprocessAdapter(
                start_cmd="python agent.py",
                env={"GAUNTLET_TRACES_URL": "/tmp/traces.jsonl"},
            )
            adapter.start()

            call_kwargs = mock_popen.call_args[1]
            assert call_kwargs["env"]["GAUNTLET_TRACES_URL"] == "/tmp/traces.jsonl"

    def test_stop_terminates_process(self) -> None:
        mock_proc = MagicMock()

        with patch("gauntlet.adapters.subprocess_adapter.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc

            adapter = SubprocessAdapter(start_cmd="python agent.py")
            adapter.start()
            adapter.stop()

            mock_proc.terminate.assert_called_once()
            mock_proc.wait.assert_called_once_with(timeout=5)

    def test_stop_kills_on_timeout(self) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="python", timeout=5),
            None,  # second call after kill() succeeds
        ]

        with patch("gauntlet.adapters.subprocess_adapter.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock_proc

            adapter = SubprocessAdapter(start_cmd="python agent.py")
            adapter.start()
            adapter.stop()

            mock_proc.terminate.assert_called_once()
            mock_proc.kill.assert_called_once()

    def test_stop_noop_before_start(self) -> None:
        adapter = SubprocessAdapter(start_cmd="python agent.py")
        adapter.stop()  # Should not raise

    def test_health_check_success(self) -> None:
        mock_proc = MagicMock()
        mock_response = MagicMock(status_code=200)

        with (
            patch("gauntlet.adapters.subprocess_adapter.subprocess.Popen") as mock_popen,
            patch("gauntlet.adapters.subprocess_adapter.httpx.get") as mock_get,
        ):
            mock_popen.return_value = mock_proc
            mock_get.return_value = mock_response

            adapter = SubprocessAdapter(
                start_cmd="python agent.py",
                health_url="http://localhost:8000/health",
            )
            adapter.start()  # Should not raise

            mock_get.assert_called()

    def test_health_check_timeout(self) -> None:
        mock_proc = MagicMock()

        with (
            patch("gauntlet.adapters.subprocess_adapter.subprocess.Popen") as mock_popen,
            patch("gauntlet.adapters.subprocess_adapter.httpx.get") as mock_get,
            patch("gauntlet.adapters.subprocess_adapter.time.sleep"),
        ):
            mock_popen.return_value = mock_proc
            mock_get.side_effect = httpx.ConnectError("refused")

            adapter = SubprocessAdapter(
                start_cmd="python agent.py",
                health_url="http://localhost:8000/health",
                startup_timeout=200,  # 200ms = very short timeout
            )
            with pytest.raises(TimeoutError, match="health check failed"):
                adapter.start()

            mock_proc.terminate.assert_called()
