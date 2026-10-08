"""Keeps sudo's password ticket alive while an interactive update runs.

The ticket lives a few minutes and an update can take hours; the stores that run after the first (snap, the AUR
with `--sudoflags -n`) would then find no root. One `sudo -n -v` a minute, on a thread of its own, renews it.
"""

from __future__ import annotations

import threading
from types import TracebackType

from .shell import Shell

EVERY = 60.0  # seconds between two renewals
NOTICE = "sudo ticket expired during the update; run arch-update again and type the password (unattended: arch-update auto --sudoers)"


class Keepalive:
    """A context manager: renews the ticket for as long as the block runs, and stops with it on every way out."""

    def __init__(self, sh: Shell, enabled: bool = True, every: float = EVERY) -> None:
        self.sh, self.enabled, self.every = sh, enabled, every
        self.expired = False  # a renewal was refused: later root commands of this run had no ticket
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Keepalive:
        # Only a ticket needs keeping. If `sudo -n -v` is refused now, sudo runs on a rule and there is nothing to renew.
        if self.enabled and self.sh.capture(["sudo", "-n", "-v"], timeout=10).returncode == 0:
            self._thread = threading.Thread(target=self._renew, daemon=True)
            self._thread.start()
        return self

    def _renew(self) -> None:
        while not self._stop.wait(self.every):
            if self.sh.capture(["sudo", "-n", "-v"], timeout=10).returncode != 0:
                self.expired = True
                return

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None) -> None:
        self._stop.set()  # an exception or Ctrl-C passes through: the thread is stopped first, then it goes on
        if self._thread:
            self._thread.join(timeout=15)  # the longest a renewal can run is its own 10 second limit
