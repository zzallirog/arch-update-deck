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

from . import atomic, aur_root, pkgbuild_scan
from .shell import Shell

AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
AUR_MIN_AGE_DAYS = 7
# A VCS package builds whatever its upstream holds at build time: no age of the AUR entry says anything about that.
VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr", "-darcs", "-cvs", "-nightly")
MAINTAINERS = "aur-maintainers.json"
# Packages the root shim refused, with why and the version it refused (written by aur_handoff, next to MAINTAINERS).
BY_HAND = "aur-by-hand.json"


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
    JSON file; an update by hand is what accepts the new maintainer). Also never
    built unasked: an update the root shim would refuse anyway, judged by where the
    installed version lies (root_reads) or because the shim refused it once
    (BY_HAND beside `state`; a newer version of the package gets a new try, or delete the entry),
    or an update with a dependency that is not installed and that the shim cannot take (a plain repository name only).
    With `scan`, every other waiting update has its PKGBUILD and .install read
    first (pkgbuild_scan.py); a red flag in them holds it the same way, with the
    flag as its reason.
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
    learned = read_by_hand(state.parent / BY_HAND)
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
        elif why := set_aside(learned.get(name), str(item.get("Version"))):
            unsafe.append(f"{name}: {why}")
        elif missing := missing_dependencies(sh, item):
            unsafe.append(f"{name}: needs {', '.join(missing[:6])}{' and more' if len(missing) > 6 else ''} installed first (the root shim takes only plain repository names)")
        elif where := root_reads(name, installed_paths(sh, name)):
            unsafe.append(f"{name}: installs {where}, where root reads; the shim refuses that")
        elif scan and name not in fresh:
            to_read.setdefault(base, []).append(name)
        seen.setdefault(base, maintainer)  # first sight: an orphan is kept as None, so an adoption is a change
    for base, waiting in to_read.items():
        flags = pkgbuild_scan.review(sh, base)
        unsafe += [f"{name}: {'; '.join(flags)}" for name in waiting if flags]
    atomic.write(state, json.dumps(seen, indent=1, sort_keys=True))
    return fresh, sorted(unsafe)


def missing_dependencies(sh: Shell, item: dict) -> list[str]:
    """What the AUR entry lists as depends, makedepends and checkdepends that is not installed and that the shim cannot install (pacman -T, -Si).

    The shim takes a repository package only by a plain name that a sync database holds under that name (pacman -Si):
    a version range (libfoo>=2) or a name that only another package provides is refused, so it counts as missing.
    Another AUR package would be built and handed over as a new install, which the shim refuses too. The hand-over
    installs updates only: the batch would end after the build, at that request.
    An answer that is not "all present" or "these are missing" counts as "nothing missing".
    """
    wanted = [dep for key in ("Depends", "MakeDepends", "CheckDepends") for dep in (item.get(key) or []) if isinstance(dep, str)]
    if not wanted:
        return []
    answer = sh.capture(["pacman", "-T", "--", *wanted], timeout=60)
    if answer.returncode != 127:
        return []
    return [dep for dep in answer.output.split() if not (aur_root.DEP_NAME.fullmatch(dep) and sh.capture(["pacman", "-Si", "--", dep], timeout=60).returncode == 0)]


def read_by_hand(path: Path) -> dict:
    """The set-aside list, or {} when the file is missing, unreadable or not a JSON object: a bad file must not stop a run."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def installed_paths(sh: Shell, name: str) -> list[str]:
    """What pacman lists for an installed package; nothing when pacman failed (an error line is not a path)."""
    listing = sh.capture(["pacman", "-Qlq", "--", name], timeout=60)
    return listing.output.splitlines() if listing.returncode == 0 else []


def set_aside(entry: object, candidate: str) -> str | None:
    """Why a package the shim refused still waits for a person, or None when a newer version deserves a new try.

    An entry is the reason alone (every version waits) or {"why", "version"} (only that version waits: the
    maintainer may have fixed the layout, and a refusal no longer sinks the batch, so trying again is cheap).
    """
    if isinstance(entry, dict) and entry.get("version") in (None, candidate):
        entry = entry.get("why")
    elif isinstance(entry, dict):
        return None
    if not isinstance(entry, str):
        return None
    # The text reaches the screen and the report: control characters (an escape sequence, a line break) are dropped.
    return "".join(c for c in entry if c.isprintable()) or "set aside after the shim refused it"


def root_reads(name: str, paths: list[str]) -> str | None:
    """The first installed path of `name` outside the shim's allowed places, or None.

    An update lands where the installed version lies: a package with a Qt plugin, a CUPS
    filter or a Python module there is refused by the shim every time, after the build,
    and takes the whole batch down with it. Same rules as aur_root.inspect.
    """
    folders = aur_root.own_folders(name, paths)
    for path in paths:
        clean = path.strip("/")
        if not clean:
            continue
        if path.endswith("/"):
            if not (aur_root.allowed(clean + "/", folders) or aur_root.ancestor(clean, folders)):
                return clean
        elif not aur_root.allowed(clean, folders):
            return clean
    return None


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
