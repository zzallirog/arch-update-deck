"""Room for this update on each filesystem it writes to: what it downloads, what it adds, a kernel's boot files.

The repository queue is read the way `checkupdates` reads it, into a database
copy of this tool's own (never /var/lib/pacman, and not the copy Update Day's
queue uses, so the two never wait on one lock). pacman then tells the download
and installed sizes of the new versions from that copy, and the installed sizes
of the old ones. What an update frees is credited against what it adds.

A queue that cannot be read leaves the one fixed floor on `/`; nothing here
guesses a size.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .engine import SOFT_FREE_BYTES
from .shell import Shell

GiB = 1024**3
FLOOR = SOFT_FREE_BYTES  # free on / before any update, whatever it brings
CUSHION = 512 * 1024**2  # on top of what the update needs, on every filesystem it writes to ...
CUSHION_SHARE = 0.05  # ... or this share of the filesystem when that is less: a /boot of 1 GiB keeps 51 MiB spare
PRESETS = Path("/etc/mkinitcpio.d")  # a package with a preset here is a kernel: its image and initramfs go to /boot
BOOT = Path("/boot")
CACHE = "/var/cache/pacman/pkg/"
DATABASE = "/var/lib/pacman/"
LOW = "ROOT-SPACE-LOW"  # the case whose repair trims the package cache
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
SIZE = re.compile(r"^(Name|Download Size|Installed Size)\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class Plan:
    names: list[str] = field(default_factory=list)
    download: int = 0
    growth: int = 0  # installed size of the new versions minus the old ones; below 0 when the update frees space
    boot: int = 0
    read: bool = True
    note: str = ""


def size(text: str) -> int:
    number, _, unit = text.partition(" ")
    return int(float(number) * UNITS[unit.strip()])


def _fields(listing: str) -> dict[str, dict[str, int]]:
    """Name -> sizes from `pacman -Si` or `-Qi`. A name listed by several repositories keeps the first, as pacman does."""
    found: dict[str, dict[str, int]] = {}
    for block in re.split(r"\n\s*\n", listing):
        values = dict(SIZE.findall(block))
        name = values.pop("Name", None)
        if name and name not in found:
            found[name] = {key: size(value) for key, value in values.items()}
    return found


def database() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "arch-updater" / "checkup-db"


def plan(sh: Shell, db: Path | None = None) -> Plan:
    """What the waiting repository update will download and add, read from a fresh copy of the sync databases."""
    if not sh.has("checkupdates"):
        return Plan(read=False, note="checkupdates is not installed")
    db = db or database()
    queue = sh.capture(["env", f"CHECKUPDATES_DB={db}", "LC_ALL=C", "checkupdates", "--nocolor"], timeout=180)
    if queue.returncode == 2:
        return Plan()
    if queue.returncode != 0:
        return Plan(read=False, note=f"checkupdates exited {queue.returncode}")
    names = [line.split()[0] for line in queue.output.splitlines() if " -> " in line]
    if not names:
        return Plan()
    new = sh.capture(["env", "LC_ALL=C", "pacman", "-Si", "--dbpath", str(db), "--", *names], timeout=60)
    old = sh.capture(["env", "LC_ALL=C", "pacman", "-Qi", "--", *names], timeout=60)
    try:
        coming, going = _fields(new.output), _fields(old.output)
        download = sum(coming[name]["Download Size"] for name in names)
        growth = sum(coming[name]["Installed Size"] for name in names) - sum(going.get(name, {}).get("Installed Size", 0) for name in names)
    except (KeyError, ValueError) as exc:
        return Plan(names, read=False, note=f"pacman did not tell the size of {exc}")
    boot = sum(_boot_files(name) for name in names if (PRESETS / f"{name}.preset").is_file())
    return Plan(names, download, growth, boot)


def _boot_files(kernel: str) -> int:
    """A kernel being replaced writes its image and initramfs again: they must fit once more."""
    names = (f"vmlinuz-{kernel}", f"initramfs-{kernel}.img", f"initramfs-{kernel}-fallback.img")
    return sum((BOOT / name).stat().st_size for name in names if (BOOT / name).is_file())


def _existing(path: str) -> str:
    folder = Path(path)
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return str(folder)


def _first(sh: Shell, key: str, default: str) -> str:
    answer = sh.capture(["pacman-conf", key], timeout=10)
    lines = [line.strip() for line in answer.output.splitlines() if line.strip().startswith("/")] if answer.returncode == 0 else []
    return lines[0] if lines else default


def shortfalls(sh: Shell, plan: Plan) -> list[tuple[str, str]]:
    """(case, detail) for each filesystem this update writes to that is read-only or has too little room; [] when all fit.

    `/` keeps its floor of 6 GiB. Every other filesystem is looked at only when
    the update writes to it: the pacman cache and database always, /usr for
    what it adds, /boot for a kernel's files.
    """
    cache, db = _first(sh, "CacheDir", CACHE), _first(sh, "DBPath", DATABASE)
    places = [("/", 0, True), (db, 0, True), (cache, plan.download, True), ("/usr", plan.growth, False), ("/boot", plan.boot, False)]
    devices: dict[int, dict] = {}
    for path, need, always in places:
        where = _existing(path)
        entry = devices.setdefault(sh.device(where), {"label": path.rstrip("/") or "/", "where": where, "need": 0, "look": False, "paths": set()})
        entry["need"] += need
        entry["look"] = entry["look"] or always or need > 0
        entry["paths"].add(path)
    found: list[tuple[str, str]] = []
    for entry in devices.values():
        if not entry["look"]:
            continue
        label, system = entry["label"], "/" in entry["paths"]
        if sh.read_only(entry["where"]):
            found.append(("READ-ONLY-FILESYSTEM", f"{label} is mounted read-only; nothing can be updated on it"))
            continue
        spare = min(CUSHION, int(sh.total_bytes(entry["where"]) * CUSHION_SHARE))
        need = entry["need"] + spare if entry["need"] > 0 else 0
        required = max(FLOOR, need) if system else need
        free = sh.free_bytes(entry["where"])
        if free >= required:
            continue
        case = LOW if system or cache in entry["paths"] else "MOUNT-SPACE-LOW"
        if required == FLOOR:
            found.append((case, f"{free / GiB:.1f} GiB free on /"))
        else:
            found.append((case, f"{amount(free)} free on {label}, this update needs {amount(required)} ({_why(plan, entry['paths'], cache, spare)})"))
    return found


def amount(count: int) -> str:
    return f"{count / GiB:.1f} GiB" if abs(count) >= GiB else f"{count / 1024**2:.0f} MiB"


def _why(plan: Plan, paths: set[str], cache: str, spare: int) -> str:
    parts = []
    if cache in paths and plan.download:
        parts.append(f"{amount(plan.download)} to download")
    if "/usr" in paths and plan.growth:
        parts.append(f"{'+' if plan.growth > 0 else ''}{amount(plan.growth)} installed")
    if "/boot" in paths and plan.boot:
        parts.append(f"{amount(plan.boot)} of kernel boot files")
    return ", ".join([*parts, f"{amount(spare)} spare"])
