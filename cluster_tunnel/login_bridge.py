"""Hand ``login --interactive`` to the front end that started the agent.

A front end (such as the ctun VS Code extension, in each of its windows) serves
a local pipe and names it in ``CTUN_LOGIN_BRIDGE``. Agents it starts inherit the
variable, so their ``ctun login -i`` asks the front end instead of popping up a
dialog. The front end asks the human and logs in with its own
``ctun login --credentials-stdin``; only the outcome comes back over the pipe, so
the secrets never reach the process the agent started.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from typing import BinaryIO, Optional

BRIDGE_VARIABLE = "CTUN_LOGIN_BRIDGE"


class Unavailable(Exception):
    """No front end took the login; the caller falls back to the popup."""


@dataclass
class Outcome:
    """``status`` is ``live``, ``cancelled`` or ``failed``; ``message`` explains a failure."""

    status: str
    message: str = ""


def log_in(cluster_name: str, default_limit: Optional[float]) -> Outcome:
    """Ask the front end to log in to ``cluster_name`` and wait for its outcome.

    Raises :class:`Unavailable` when no window is named, it can't be reached,
    or it declines because its user prefers the popup.
    """
    address = os.environ.get(BRIDGE_VARIABLE)
    if not address:
        raise Unavailable
    request = {"cluster": cluster_name, "session_limit": default_limit}
    try:
        with _connect(address) as pipe:
            pipe.write(json.dumps(request).encode("utf-8") + b"\n")
            reply = pipe.readline()
    except OSError:
        raise Unavailable from None
    if not reply:
        raise Unavailable
    try:
        data = json.loads(reply)
    except ValueError:
        raise Unavailable from None
    return Outcome(status=data.get("status", ""), message=data.get("message", ""))


def _connect(address: str) -> BinaryIO:
    """Open the extension's named pipe (Windows) or Unix socket for reading and writing."""
    if sys.platform == "win32":
        return open(address, "r+b", buffering=0)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(address)
    return sock.makefile("rwb", buffering=0)
