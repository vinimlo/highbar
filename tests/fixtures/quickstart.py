"""The quickstart that `highbar init` copies, on a port of its own in each test (HB-40).

The quickstart's agent listens on 8731, and that is what a person runs and what the docs
show. Tests start it from several places, and the build node runs several suites at
once: on one fixed port, one run took the other's port and failed.
"""

from __future__ import annotations

import socket
from pathlib import Path

QUICKSTART_PORT = 8731
_FILES = ("agent.py", "specs/quickstart.highbar.yaml")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def move_to_a_free_port(directory: Path) -> int:
    """Point the agent and the spec that `highbar init` wrote at a free port."""
    port = free_port()
    for relative in _FILES:
        path = directory / relative
        text = path.read_text(encoding="utf-8")
        # If the quickstart stops saying 8731, this has to learn its new port.
        assert str(QUICKSTART_PORT) in text, f"{relative} no longer says {QUICKSTART_PORT}"
        path.write_text(text.replace(str(QUICKSTART_PORT), str(port)), encoding="utf-8")
    return port
