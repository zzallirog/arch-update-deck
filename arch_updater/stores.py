"""The stores a machine can have, and the one command that updates each.

Whatever is installed gets updated; a store whose tool is absent does not
exist here. No queue is read: a store changed something if its package listing
differs afterwards.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import atomic, pkgbuild_scan
from .shell import Shell

AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
AUR_MIN_AGE_DAYS = 7
# A VCS package builds whatever its upstream holds at build time: no age of the AUR entry says anything about that.
VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr", "-darcs", "-cvs", "-nightly")
MAINTAINERS = "aur-maintainers.json"


@dataclass(frozen=True)
class Store:
    name: str
    tool: str
    listing: tuple[str, ...]
    update: tuple[str, ...]
    root: bool = False


STORES = (
    Store("repo", "pacman", ("pacman", "-Q"), ("pacman", "-Syu", "--noconfirm"), root=True),
    Store(
        "aur",
        "yay",
        ("pacman", "-Qm"),
        ("yay", "-Sua", "--noconfirm", "--answerclean", "None", "--answerdiff", "None", "--answeredit", "None"),
    ),
    Store("aur", "paru", ("pacman", "-Qm"), ("paru", "-Sua", "--noconfirm", "--skipreview", "--sudoflags", "-n")),
    Store("flatpak", "flatpak", ("flatpak", "list", "--columns=ref,active"), ("flatpak", "update", "-y", "--noninteractive")),
    Store("snap", "snap", ("snap", "list"), ("snap", "refresh"), root=True),
)


def aur_too_fresh(sh: Shell, min_age_days: float) -> list[str]:
    return aur_review(sh, min_age_days)[0]


def aur_review(sh: Shell, min_age_days: float, state: Path | None = None, scan: bool = False) -> tuple[list[str], list[str]]:
    """Foreign packages whose AUR entry changed within the last few days.

    A poisoned AUR package is usually reported and removed within days; holding
    an update back that long lets someone else find it first. It is a cool-down,
    not a proof: what nobody noticed in time still gets through.

    Returns the fresh names, and the waiting updates that are never built unasked,
    each as "name: reason": a VCS package, an orphan, and an entry whose
    maintainer changed since the last time it was seen up to date (`state`, a
    JSON file; an update by hand is what accepts the new maintainer). With `scan`,
    every other waiting update has its PKGBUILD and .install read first
    (pkgbuild_scan.py); a red flag in them holds it the same way, with the flag
    as its reason.
    """
    foreign = sh.capture(["pacman", "-Qqm"], timeout=60)
    if foreign.returncode not in (0, 1):  # 1: no foreign packages
        raise OSError(f"pacman -Qqm failed: {foreign.output.strip()[-200:]}")
    names = foreign.output.split()
    if not names:
        return [], []
    answer = sh.post(AUR_RPC, [("arg[]", name) for name in names])
    if answer.get("type") != "multiinfo" or answer.get("error"):  # an error document must not read as "nothing is fresh"
        raise ValueError(f"AUR answered {answer.get('type')}: {answer.get('error')}")
    results = answer["results"]
    if answer.get("resultcount", len(results)) != len(results):  # a cut-off answer lists fewer than it counts
        raise ValueError(f"AUR answer is incomplete: counts {answer['resultcount']}, lists {len(results)}")
    cutoff = time.time() - min_age_days * 86400
    fresh = sorted(item["Name"] for item in results if item["LastModified"] > cutoff)
    if state is None:
        return fresh, []
    versions = dict(line.split()[:2] for line in sh.capture(["pacman", "-Qm"], timeout=60).output.splitlines() if len(line.split()) >= 2)
    try:
        seen = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    unsafe: list[str] = []
    to_read: dict[str, list[str]] = {}  # package base -> the waiting names built from it
    for item in results:
        name, base, maintainer = item["Name"], item.get("PackageBase") or item["Name"], item.get("Maintainer")
        if name not in versions:
            continue
        if not _newer(sh, str(item.get("Version")), versions[name]):
            seen[base] = maintainer  # nothing waits: whoever maintains it now is accepted
        elif name.endswith(VCS_SUFFIXES):
            unsafe.append(f"{name}: VCS package, builds whatever upstream has now")
        elif not maintainer:
            unsafe.append(f"{name}: orphaned in the AUR")
        elif seen.get(base, maintainer) != maintainer:
            unsafe.append(f"{name}: maintainer changed from {seen[base]} to {maintainer}")
        elif scan and name not in fresh:
            to_read.setdefault(base, []).append(name)
        seen.setdefault(base, maintainer)  # first sight: an orphan is kept as None, so an adoption is a change
    for base, waiting in to_read.items():
        flags = pkgbuild_scan.review(sh, base)
        unsafe += [f"{name}: {'; '.join(flags)}" for name in waiting if flags]
    atomic.write(state, json.dumps(seen, indent=1, sort_keys=True))
    return fresh, sorted(unsafe)


def _newer(sh: Shell, candidate: str, installed: str) -> bool:
    """pacman's own comparison; an answer that is not a number counts as "not newer"."""
    result = sh.capture(["vercmp", candidate, installed], timeout=10)
    try:
        return int(result.output.strip()) > 0
    except ValueError:
        return False


def ignored(sh: Shell) -> list[str]:
    """Updates pacman left out because pacman.conf says IgnorePkg or IgnoreGroup: a partial update, named.

    Read right after `pacman -Syu`, when the sync databases are fresh: `pacman -Qu`
    marks each such package `[ignored]`. HoldPkg does not hold an update back; it
    only guards a removal, and a run that meets it fails as HOLDPKG-IN-TARGETS.
    """
    result = sh.capture(["pacman", "-Qu"], timeout=60)
    if result.returncode not in (0, 1):  # 1: nothing to upgrade
        return []
    return [line.removesuffix("[ignored]").strip() for line in result.output.splitlines() if line.rstrip().endswith("[ignored]")]
