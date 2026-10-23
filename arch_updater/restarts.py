"""After an update: which running programs still use a library or program the update replaced.

pacman replaces a file by writing a new one and unlinking the old; a process that
had the old one mapped keeps it, and /proc/<pid>/maps then names it with
" (deleted)". Such a process runs the old code until it is restarted. This only
reads: nothing here restarts, stops or signals anything.

The kernel shows a process's maps only to its own account (and to uid 0), so a
run as the desktop user sees its own processes and user services. The others,
PID 1 and the system services among them, are counted as `unread`; a scan with
anything unread says so and never claims the machine is clean.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROC = Path("/proc")
# Trees pacman installs into; a deleted file elsewhere (/tmp, /dev/shm, memfd:) is not the update's doing.
PACKAGED = ("/usr/", "/opt/")
LIBRARY = re.compile(r"\.so(?:\.[0-9][0-9.]*)?$")
# The manager and the bus: a new copy of either only starts with a new boot (or, for the user's, a new login).
CORE = frozenset({"systemd", "dbus-daemon", "dbus-broker", "dbus-broker-lau"})  # comm is cut at 15 characters
USER_MANAGER = re.compile(r"/user@(\d+)\.service(?:/|$)")


@dataclass
class Restarts:
    system: list[str] = field(default_factory=list)  # system services: sudo systemctl restart ...
    user: list[str] = field(default_factory=list)  # user services: systemctl --user restart ...
    programs: list[str] = field(default_factory=list)  # anything else: close it and start it again
    reboot: bool = False
    why: list[str] = field(default_factory=list)  # what makes the reboot needed
    unread: int = 0  # processes whose maps this account may not read
    complete: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def stale(maps: str) -> list[str]:
    """The packaged files a process still maps after they were replaced: libraries, and code mapped executable."""
    found = []
    for line in maps.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 6 or not fields[5].endswith(" (deleted)"):
            continue
        path = fields[5].removesuffix(" (deleted)")
        if path.startswith(PACKAGED) and (LIBRARY.search(path) or "x" in fields[1]):
            found.append(path)
    return sorted(set(found))


def unit_of(cgroup: str) -> tuple[str, str]:
    """("system" | "user", unit) for a process in a service, or ("", "") for one in a scope or nowhere known."""
    for line in cgroup.splitlines():
        _, _, path = line.partition("::") if line.startswith("0::") else line.rpartition(":")
        parts = [part for part in path.split("/") if part]
        services = [index for index, part in enumerate(parts) if part.endswith(".service")]
        if not services or parts[-1].endswith(".scope"):
            continue
        unit = parts[services[-1]]
        if USER_MANAGER.search(path):
            return ("user", unit) if not USER_MANAGER.fullmatch("/" + unit) else ("", "")
        return "system", unit
    return "", ""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""  # it ended while being read: nothing of it is left to restart
    except OSError:  # another account's process (EACCES), or one the kernel would not show
        return None


def scan(proc: Path | None = None) -> Restarts:
    """Every process that runs replaced code, sorted into what a person restarts and whether only a reboot helps."""
    proc = proc or PROC
    found = Restarts()
    system: set[str] = set()
    user: set[str] = set()
    programs: set[str] = set()
    try:
        pids = sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit())
    except OSError:
        return Restarts(unread=0, complete=False)
    for pid in pids:
        maps = _read(proc / str(pid) / "maps")
        if maps is None:
            found.unread += 1
            continue
        files = stale(maps)
        if not files:
            continue
        comm = (_read(proc / str(pid) / "comm") or "").strip() or str(pid)
        if pid == 1 or comm in CORE:
            found.reboot = True
            found.why.append(f"{comm} (pid {pid}) uses the replaced {Path(files[0]).name}")
            continue
        scope, unit = unit_of(_read(proc / str(pid) / "cgroup") or "")
        (system if scope == "system" else user if scope == "user" else programs).add(unit or comm)
    found.system, found.user, found.programs = sorted(system), sorted(user), sorted(programs)
    found.complete = found.unread == 0
    return found


def commands(found: dict[str, Any]) -> list[str]:
    """The commands a person runs for what a scan found; this tool runs none of them."""
    said = []
    if found.get("system"):
        said.append("sudo systemctl restart " + " ".join(found["system"]))
    if found.get("user"):
        said.append("systemctl --user restart " + " ".join(found["user"]))
    return said
