"""The one door between the unattended run and the machine.

Every command, lookup, wait and network call goes through a Shell, so a test
replaces the whole machine by handing in a scripted one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .engine import CommandResult, run_capture

# Programs that change the package set or the boot files. A signal in the middle of one leaves a
# half-applied transaction, so they are never given a time limit, whatever the caller asks for.
TRANSACTION_TOOLS = frozenset({"pacman", "yay", "paru", "paccache", "mkinitcpio", "dkms", "snap", "flatpak", "grub-mkconfig"})
# Terminals, and what each wants in front of the command it should run.
TERMINALS = {
    "xdg-terminal-exec": [],
    "kitty": [],
    "foot": [],
    "wezterm": ["start", "--"],
    "alacritty": ["-e"],
    "konsole": ["-e"],
    "gnome-terminal": ["--"],
    "xterm": ["-e"],
}


# Where a command that sudo will run as root may come from. Never the caller's PATH:
# the account that is being granted the rule controls that.
ROOT_PATH = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")


class Shell:
    def has(self, tool: str) -> bool:
        return shutil.which(tool) is not None

    def path(self, tool: str) -> str:
        return shutil.which(tool) or tool

    def root_binary(self, tool: str, dirs: tuple[str, ...] = ROOT_PATH) -> tuple[str | None, str]:
        """The path sudo may be told to run `tool` at, or None and the reason there is none.

        Only the fixed `dirs` are searched. The file must be owned by root and must not
        be writable by its group or by anyone else, or the rule would hand root to whoever can write it.
        """
        for directory in dirs:
            candidate = Path(directory) / tool
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            info = candidate.stat()  # follows links: what counts is the file that would run
            if info.st_uid != 0:
                return None, f"{candidate} is not owned by root"
            if info.st_mode & 0o022:
                return None, f"{candidate} is writable by its group or by others"
            return str(candidate), ""
        return None, f"{tool} not found in {':'.join(dirs)}"

    def capture(self, argv: list[str], timeout: int = 30) -> CommandResult:
        """Run a short read-only command and return its output."""
        return run_capture(argv, timeout=timeout)

    def logged(self, argv: list[str], log: Path, timeout: int | None = None) -> CommandResult:
        """Run a command that changes the machine, with its output going straight to the log.

        The command line is on disk before the command starts and its output as
        it happens, so a run that dies mid-transaction still leaves what it was doing.

        A timeout is only for helpers we own (a backup archive, say). It is ignored when the
        command is a package or boot transaction: the signal would reach it through sudo and
        cut it off half way, which is worse than waiting.
        """
        if timeout is not None and TRANSACTION_TOOLS & {Path(word).name for word in argv}:
            timeout = None
        with log.open("ab") as handle:
            handle.write(f"\n$ {' '.join(argv)}\n".encode())
            handle.flush()
            start = handle.tell()
            try:
                # Its own session, so a timeout can reach the whole tree and not just the first child.
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            except FileNotFoundError:
                returncode = 127
            else:
                try:
                    returncode = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # Only reached for commands that are not transactions (see above).
                    with contextlib.suppress(PermissionError, ProcessLookupError):
                        os.killpg(child.pid, signal.SIGTERM)
                    child.wait()
                    returncode = 124
            handle.write(f"\n[exit {returncode}]\n".encode())
        with log.open("rb") as handle:
            handle.seek(start)
            return CommandResult(list(argv), returncode, handle.read().decode("utf-8", errors="replace"))

    def terminal(self) -> list[str] | None:
        """The command that opens a terminal running what follows it, or None if there is none."""
        asked = os.environ.get("TERMINAL")
        for name, flags in ((asked, TERMINALS.get(asked or "", ["-e"])), *TERMINALS.items()):
            if name and self.has(name):
                return [name, *flags]
        return None

    def free_bytes(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def resolves(self, host: str = "archlinux.org") -> bool:
        try:
            socket.getaddrinfo(host, 443)
        except OSError:
            return False
        return True

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def post(self, url: str, fields: list[tuple[str, str]]) -> Any:
        """POST a form to an https API and return its JSON answer."""
        if not url.startswith("https://"):
            raise ValueError(f"refusing a non-https URL: {url}")
        data = urllib.parse.urlencode(fields).encode()
        with urllib.request.urlopen(url, data=data, timeout=30) as response:  # noqa: S310 - https checked above
            return json.load(response)
