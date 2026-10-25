"""Tests for endpoint parsing and port probing."""

from __future__ import annotations

import os
import shutil
import socket

import pytest

from highbar.adapters.ports import accepts_connections, describe_listener, endpoint_address


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEndpointAddress:
    def test_endpoint_address_defaults(self) -> None:
        assert endpoint_address("http://x/chat") == ("x", 80)
        assert endpoint_address("https://x/chat") == ("x", 443)

    def test_explicit_port(self) -> None:
        assert endpoint_address("http://localhost:8111/chat") == ("localhost", 8111)


class TestProbing:
    def test_free_port_does_not_accept(self) -> None:
        assert not accepts_connections("127.0.0.1", _free_port())

    def test_listening_port_accepts(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            assert accepts_connections("127.0.0.1", server.getsockname()[1])

    @pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
    def test_describe_listener_names_the_process(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            description = describe_listener(server.getsockname()[1])

        assert description is not None
        assert f"pid {os.getpid()}" in description

    def test_describe_listener_of_a_free_port_is_none(self) -> None:
        assert describe_listener(_free_port()) is None
