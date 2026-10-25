"""Where the agent listens, and whether something already does."""

from __future__ import annotations

import shutil
import socket
import subprocess
from urllib.parse import urlsplit


def endpoint_address(url: str) -> tuple[str, int]:
    """The host and port of an endpoint URL; the port defaults by scheme (80, 443)."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname or "localhost", port


def accepts_connections(host: str, port: int, timeout: float = 0.5) -> bool:
    """Whether anything accepts a TCP connection on ``host:port``.

    Every address the name resolves to is tried: ``localhost`` is ``::1`` and
    ``127.0.0.1``, and an agent may listen on only one of them.
    """
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for family, kind, proto, _, address in addresses:
        with socket.socket(family, kind, proto) as sock:
            sock.settimeout(timeout)
            try:
                sock.connect(address)
            except OSError:
                continue
            return True
    return False


def describe_listener(port: int) -> str | None:
    """``"<command> (pid N)"`` for whatever listens on ``port``, if ``lsof`` can tell."""
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    try:
        output = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    pid = command = None
    for line in output.splitlines():
        if line.startswith("p") and pid is None:
            pid = line[1:]
        elif line.startswith("c") and command is None:
            command = line[1:]
    if pid is None:
        return None
    return f"{command} (pid {pid})" if command else f"pid {pid}"
