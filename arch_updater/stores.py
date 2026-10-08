"""The stores a machine can have, and the one command that updates each.

Whatever is installed gets updated; a store whose tool is absent does not
exist here. No queue is read: a store changed something if its package listing
differs afterwards.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .shell import Shell

AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"
AUR_MIN_AGE_DAYS = 4
# What yay itself asks sudo for when it installs what it built.
YAY_SUDO = (
    "pacman -U --noconfirm --config /etc/pacman.conf -- {cache}/*",
    "pacman -D -q --asexplicit --noconfirm --config /etc/pacman.conf -- *",
    "pacman -D -q --asdeps --noconfirm --config /etc/pacman.conf -- *",
    "pacman -S --noconfirm --config /etc/pacman.conf --asdeps -- *",
)


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
        ("yay", "-Sua", "--noconfirm", "--answerclean", "None", "--answerdiff", "None", "--answeredit", "None", "--sudoflags", "-n"),
    ),
    Store("aur", "paru", ("pacman", "-Qm"), ("paru", "-Sua", "--noconfirm", "--skipreview", "--sudoflags", "-n")),
    Store("flatpak", "flatpak", ("flatpak", "list", "--columns=ref,active"), ("flatpak", "update", "-y", "--noninteractive")),
    Store("snap", "snap", ("snap", "list"), ("snap", "refresh"), root=True),
)


def aur_too_fresh(sh: Shell, min_age_days: float) -> list[str]:
    """Foreign packages whose AUR entry changed within the last few days.

    A poisoned AUR package is usually reported and removed within days; holding
    an update back that long lets someone else find it first. It is a cool-down,
    not a proof: what nobody noticed in time still gets through.
    """
    foreign = sh.capture(["pacman", "-Qqm"], timeout=60)
    if foreign.returncode not in (0, 1):  # 1: no foreign packages
        raise OSError(f"pacman -Qqm failed: {foreign.output.strip()[-200:]}")
    names = foreign.output.split()
    if not names:
        return []
    answer = sh.post(AUR_RPC, [("arg[]", name) for name in names])
    if answer.get("type") != "multiinfo":  # an error document must not read as "nothing is fresh"
        raise ValueError(f"AUR answered {answer.get('type')}: {answer.get('error')}")
    results = answer["results"]
    cutoff = time.time() - min_age_days * 86400
    return sorted(item["Name"] for item in results if item["LastModified"] > cutoff)
