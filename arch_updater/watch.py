"""A look at the machine as it is now: one probe per question, all at once.

A probe only reads. It answers with verdicts and, where it learned something
the run needs, with facts; it never changes the world or anything shared, so
the probes run side by side and one that breaks does not stop the others.
Every verdict that is not a pass names a case in the census.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import exits, repairs
from .engine import SOFT_FREE_BYTES, attest, snapshot
from .shell import Shell


@dataclass
class Verdict:
    probe: str
    status: str  # pass | fail | blocked | unknown
    case: str = ""  # the census case a non-pass belongs to
    detail: str = ""
    code: int = exits.BLOCKED  # the exit this verdict leaves behind when it blocks


@dataclass
class Reading:
    verdicts: list[Verdict]
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scene:
    """What a probe may read."""

    sh: Shell
    config: dict[str, Any] | None
    config_path: Path
    keep: Path
    baseline: dict[str, Any] | None  # the machine at the first look of this run


def passed(probe: str, detail: str = "") -> Reading:
    return Reading([Verdict(probe, "pass", detail=detail)])


def probe_privilege(scene: Scene) -> Reading:
    if os.geteuid() == 0:
        return Reading([Verdict("privilege", "blocked", "NO-ROOT", "run as the desktop user: makepkg refuses root", exits.TOOL_FAILURE)])
    if scene.sh.capture(repairs.sudo(["true"]), timeout=10).returncode != 0:
        detail = "sudo -n needs a password; if the rules are installed, their file must sort last in /etc/sudoers.d"
        return Reading([Verdict("privilege", "blocked", "NO-ROOT", detail, exits.TOOL_FAILURE)])
    return passed("privilege")


def probe_conditions(scene: Scene) -> Reading:
    """The owner's own guards: each configured command must exit 0 before anything changes.

    Home network, backup freshness and whatever else live here as commands, so
    this code never has to learn what "home" or "fresh" means on a machine.
    """
    if scene.config is None:
        return Reading([Verdict("conditions", "blocked", "NOT-CONFIGURED", f"no config at {scene.config_path}", exits.TOOL_FAILURE)])
    verdicts = []
    for condition in scene.config.get("conditions") or []:
        result = scene.sh.capture(condition["argv"], timeout=60)
        if result.returncode == 0:
            verdicts.append(Verdict(condition["name"], "pass"))
            continue
        code = condition.get("exit", exits.BLOCKED)
        detail = result.output.strip()[-300:] or f"exit {result.returncode}"
        verdicts.append(Verdict(condition["name"], "blocked", "CONDITION", detail, code if code in exits.CONDITION else exits.BLOCKED))
    return Reading(verdicts)


def device(path: Path) -> int:
    return path.stat().st_dev


def probe_keep(scene: Scene) -> Reading:
    root = scene.keep
    if not (scene.config or {}).get("keep"):
        return passed("keep", f"{root} (not configured: beside the run, on the system disk)")
    # The folder is made when the record is written. What has to exist already is the nearest folder above it.
    anchor = root
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    if not (anchor.is_dir() and os.access(anchor, os.W_OK)):
        return Reading([Verdict("keep", "blocked", "KEEP-UNWRITABLE", f"{root} is not a writable directory", exits.BACKUP_BLOCKED)])
    # A second disk that is not mounted leaves its mount point as a plain directory on the system disk.
    if device(anchor) == device(Path("/")) and not scene.config.get("keep_on_system_disk"):
        detail = f"{root} is on the system disk; is the other disk mounted?"
        return Reading([Verdict("keep", "blocked", "KEEP-UNWRITABLE", detail, exits.BACKUP_BLOCKED)])
    return passed("keep", str(root))


def probe_lock(scene: Scene) -> Reading:
    if repairs.PACMAN_LOCK.exists():
        return Reading([Verdict("lock", "fail", "PACMAN-LOCK", str(repairs.PACMAN_LOCK))])
    return passed("lock")


def probe_disk(scene: Scene) -> Reading:
    free = scene.sh.free_bytes("/")
    if free < SOFT_FREE_BYTES:
        return Reading([Verdict("disk", "fail", "ROOT-SPACE-LOW", f"{free / 1024**3:.1f} GiB free on /")])
    return passed("disk")


def probe_dns(scene: Scene) -> Reading:
    if not scene.sh.resolves():
        return Reading([Verdict("dns", "fail", "DNS-FAILED", "archlinux.org does not resolve")])
    return passed("dns")


def probe_database(scene: Scene) -> Reading:
    check = scene.sh.capture(["pacman", "-Dk"], timeout=120)
    if check.returncode != 0:
        return Reading([Verdict("database", "blocked", "PACMAN-DATABASE", check.output.strip()[-300:])])
    return passed("database")


def probe_machine(scene: Scene) -> Reading:
    """Kernel, initramfs, DKMS, ABI and failed units, read through attest().

    The first look of a run is the baseline: only units that fail after it are
    blamed on the update. A pending reboot is a fact beside the verdict; it
    does not hold updates back.
    """
    state = snapshot(include_updates=False, include_slow=True)
    issues = attest(before=scene.baseline or state, after=state)["issues"]
    reboot = any(issue["reboot_required"] for issue in issues)
    facts = {"state": state, "pacnew": state.get("pacnew") or [], "reboot": reboot}
    verdicts = [Verdict("machine", "fail", issue["mode"], issue["message"]) for issue in issues if not issue["reboot_required"]]
    return Reading(verdicts or [Verdict("machine", "pass", detail="reboot pending" if reboot else "")], facts)


def probe_smoke(scene: Scene) -> Reading:
    """Run the configured commands; one that no longer starts was built against old libraries."""
    rebuild, verdicts = [], []
    for entry in (scene.config or {}).get("smoke") or []:
        program = entry["argv"][0]
        if not scene.sh.has(program) or scene.sh.capture(entry["argv"], timeout=20).returncode == 0:
            continue  # not installed is not broken
        rebuild.append(entry["rebuild"])
        verdicts.append(Verdict("smoke", "fail", "ABI-MISMATCH", f"{program} does not start; rebuild {entry['rebuild']}"))
    return Reading(verdicts or [Verdict("smoke", "pass")], {"rebuild": rebuild})


PROBES: tuple[Callable[[Scene], Reading], ...] = (
    probe_privilege,
    probe_conditions,
    probe_keep,
    probe_lock,
    probe_disk,
    probe_dns,
    probe_database,
    probe_machine,
    probe_smoke,
)


def _ask(probe: Callable[[Scene], Reading], scene: Scene) -> Reading:
    try:
        return probe(scene)
    except Exception as exc:  # a probe that breaks must still answer: unknown, never silence
        name = probe.__name__.removeprefix("probe_")
        return Reading([Verdict(name, "unknown", detail=f"{type(exc).__name__}: {exc}")])


def look(scene: Scene) -> Reading:
    """Ask every probe at once; the answer keeps the probes' own order."""
    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        readings = list(pool.map(lambda probe: _ask(probe, scene), PROBES))
    facts: dict[str, Any] = {}
    for reading in readings:
        facts.update(reading.facts)
    return Reading([verdict for reading in readings for verdict in reading.verdicts], facts)
