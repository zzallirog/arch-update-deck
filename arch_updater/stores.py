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


def aur_review(sh: Shell, min_age_days: float, state: Path | None = None) -> tuple[list[str], list[str]]:
    """Foreign packages whose AUR entry changed within the last few days.

    A poisoned AUR package is usually reported and removed within days; holding
    an update back that long lets someone else find it first. It is a cool-down,
    not a proof: what nobody noticed in time still gets through.

    Returns the fresh names, and the waiting updates that are never built unasked,
    each as "name: reason": a VCS package, an orphan, and an entry whose
    maintainer changed since the last time it was seen up to date (`state`, a
    JSON file; an update by hand is what accepts the new maintainer).
    """
    foreign = sh.capture(["pacman", "-Qqm"], timeout=60)
    if foreign.returncode not in (0, 1):  # 1: no foreign packages
        raise OSError(f"pacman -Qqm failed: {foreign.output.strip()[-200:]}")
    names = foreign.output.split()
    if not names:
        return [], []
    answer = sh.post(AUR_RPC, [("arg[]", name) for name in names])
    if answer.get("type") != "multiinfo":  # an error document must not read as "nothing is fresh"
        raise ValueError(f"AUR answered {answer.get('type')}: {answer.get('error')}")
    results = answer["results"]
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
        seen.setdefault(base, maintainer)  # first sight: an orphan is kept as None, so an adoption is a change
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")
    return fresh, sorted(unsafe)


def _newer(sh: Shell, candidate: str, installed: str) -> bool:
    """pacman's own comparison; an answer that is not a number counts as "not newer"."""
    result = sh.capture(["vercmp", candidate, installed], timeout=10)
    try:
        return int(result.output.strip()) > 0
    except ValueError:
        return False
