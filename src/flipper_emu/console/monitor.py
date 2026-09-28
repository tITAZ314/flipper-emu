"""Renode monitor client: fire commands at a running session over TCP.

The runner starts Renode with the monitor listening on a TCP port (`-P <port>`),
which is how the UI injects button presses into the firmware's GPIO lines. The
protocol is line based and the replies are ANSI-coloured, so replies are stripped
and only used for diagnostics.
"""

from __future__ import annotations

import re
import socket
import time
from typing import List, Optional

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3456

#: Fragments that mean the monitor refused a command or it failed inside Renode.  A
#: reply also carries whatever the emulator printed meanwhile, so this stays narrow on
#: purpose: button injection failing silently is what made "the keys do nothing"
#: invisible for so long.
FAILURE_FRAGMENTS = (
    "Traceback (most recent call last)",
    "Could not find",
    "Could not load",
    "Could not resolve",
    "Invalid command",
    "Unrecognized command",
    "not part of the machine",
)


def check_reply(text: str) -> str:
    """A short description if the monitor's reply looks like a failure, else ""."""
    if not text:
        return ""
    flattened = " ".join(text.split())
    for fragment in FAILURE_FRAGMENTS:
        index = flattened.find(fragment)
        if index >= 0:
            return flattened[max(0, index - 60) : index + 100]
    return ""


class MonitorError(Exception):
    """Could not talk to the Renode monitor."""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class RenodeMonitor:
    """A minimal line-oriented client for Renode's monitor."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.socket: Optional[socket.socket] = None

    # -- connection -------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.socket is not None

    def connect(self, attempts: int = 20, delay: float = 0.5) -> bool:
        """Connect, retrying while Renode is still starting up."""
        for attempt in range(attempts):
            try:
                connection = socket.create_connection((self.host, self.port), self.timeout)
                connection.settimeout(self.timeout)
                self.socket = connection
                self._drain(0.2)  # banner and prompt
                return True
            except OSError:
                if attempt == attempts - 1:
                    return False
                time.sleep(delay)
        return False

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None

    # -- commands ---------------------------------------------------------
    def command(self, text: str, wait: float = 0.15) -> str:
        """Send one monitor command and return whatever it printed."""
        if self.socket is None and not self.connect():
            raise MonitorError("monitor not reachable at %s:%d" % (self.host, self.port))
        try:
            self.socket.sendall((text + "\n").encode())
        except OSError as exc:
            raise MonitorError("send failed: %s" % exc)
        return self._drain(wait)

    def send(self, text: str, wait: float = 0.05) -> str:
        """Send one monitor command and return the reply (used for button edges).

        The reply is what makes a refused command visible: a firmware pin that never
        changed looks exactly like a key that was never pressed otherwise.
        """
        return self.command(text, wait=wait)

    def _drain(self, wait: float) -> str:
        chunks: List[str] = []
        if self.socket is None:
            return ""
        deadline = time.time() + max(wait, 0.0)
        while True:
            try:
                data = self.socket.recv(4096)
            except socket.timeout:
                break
            except OSError:
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", errors="replace"))
            if time.time() >= deadline:
                break
        return strip_ansi("".join(chunks))
