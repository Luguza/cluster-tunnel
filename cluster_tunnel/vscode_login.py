"""Hand ``login --interactive`` to the VS Code window that started the agent.

The ctun VS Code extension serves a local pipe in each window and names it in
``CTUN_VSCODE_LOGIN``. Agents started in that window inherit the variable, so
their ``ctun login -i`` asks the window instead of popping up a dialog. The
window asks the human and logs in with its own ``ctun login --credentials-stdin``;
only the outcome comes back over the pipe, so the secrets never reach the
process the agent started.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from typing import BinaryIO, Optional

BRIDGE_VARIABLE = "CTUN_VSCODE_LOGIN"


class Unavailable(Exception):
    """No VS Code window took the login; the caller falls back to the popup."""


@dataclass
class Outcome:
    """``status`` is ``live``, ``cancelled`` or ``failed``; ``message`` explains a failure."""

    status: str
    message: str = ""


def log_in(cluster_name: str, default_limit: Optional[float]) -> Outcome:
    """Ask the VS Code window to log in to ``cluster_name`` and wait for its outcome.

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
