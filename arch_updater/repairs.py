"""Automatic repairs, by the names the census gives them.

One bounded action each. A repair answers whether it was attempted, never
whether it worked: its exit code proves nothing, the next look at the machine
does, and the budget in control.tis bounds the retries.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import aur_handoff, census
from .engine import quarantine_missing_aur_caches

if TYPE_CHECKING:
    from .auto import World

PACMAN_LOCK = Path("/var/lib/pacman/db.lck")
# Root commands, exact: `arch-update auto --sudoers` grants these and nothing wider.
PACCACHE = ("paccache", "-rk2")
MKINITCPIO = ("mkinitcpio", "-P")
DKMS = ("dkms", "autoinstall")
REBUILD = ("yay", "-S", "--rebuild", "--noconfirm", "--answerclean", "All", "--answerdiff", "None")


def sudo(argv: tuple[str, ...] | list[str]) -> list[str]:
    return ["sudo", "-n", *argv]


def _wait_until(world: World, done: Callable[[], bool], tries: int, pause: float) -> None:
    for _ in range(tries):
        if done():
            return
        world.sh.sleep(pause)


def wait_for_lock(world: World) -> bool:
    """Give the lock's owner two minutes. The lock is never removed."""
    _wait_until(world, lambda: not PACMAN_LOCK.exists(), tries=24, pause=5)
    return True


def trim_cache(world: World) -> bool:
    world.command(sudo(PACCACHE))
    return True


def wait_for_dns(world: World) -> bool:
    world.command(["resolvectl", "flush-caches"])
    _wait_until(world, world.sh.resolves, tries=6, pause=10)
    return True


def wait_for_mirror(world: World) -> bool:
    world.sh.sleep(60)
    return True


def quarantine_aur_cache(world: World) -> bool:
    moved = quarantine_missing_aur_caches(world.output, world.run_id)
    world.note("quarantine", caches=moved)
    return bool(moved)


def rebuild_initramfs(world: World) -> bool:
    world.command(sudo(MKINITCPIO))
    return True


def rebuild_dkms(world: World) -> bool:
    world.command(sudo(DKMS))  # the running kernel; a kernel installed later is built by its pacman hook
    return True


def rebuild_against_new_libraries(world: World) -> bool:
    # A rebuild is an AUR build and install: it obeys the same switch as every other one.
    if not world.rebuild or (world.config or {}).get("unattended_aur") is not True or not world.sh.has("yay"):
        return False
    if not aur_handoff.shim_installed(world.sh):
        return False
    world.command([*REBUILD, *aur_handoff.flags(world.sh.path("arch-update")), *world.rebuild])
    return True


def replay(world: World) -> bool:
    """A case this machine learned: run the command that fixed it before, as the account, where it ran then."""
    case = census.at(world.cases, world.failure)
    if case is None or not case.fix.strip():
        return False
    world.note("replay", case=case.id)
    world.command(["env", "-C", os.path.expanduser(case.where or "~"), "sh", "-c", case.fix])
    return True


ACTIONS: dict[str, Callable[[World], bool]] = {
    "replay": replay,
    "wait-for-lock": wait_for_lock,
    "trim-cache": trim_cache,
    "wait-for-dns": wait_for_dns,
    "wait-for-mirror": wait_for_mirror,
    "quarantine-aur-cache": quarantine_aur_cache,
    "rebuild-initramfs": rebuild_initramfs,
    "rebuild-dkms": rebuild_dkms,
    "rebuild-against-new-libraries": rebuild_against_new_libraries,
}
