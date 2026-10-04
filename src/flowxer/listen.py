"""Listening sockets bound before the servers start, so a taken port is a clear error."""

from __future__ import annotations

import os
import socket


def bind_listener(host: str, port: int) -> socket.socket:
    """A TCP socket bound to host:port and listening. Raises OSError (e.g. EADDRINUSE)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # On Windows SO_REUSEADDR would let a second process bind the same port.
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        sock.set_inheritable(True)
    except OSError:
        sock.close()
        raise
    return sock
