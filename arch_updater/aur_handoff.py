"""What yay runs instead of sudo in an unattended AUR update: the hand-over to the root shim.

yay is told `--sudo arch-update --sudoflags aur-handoff`, so where it would run
`sudo pacman -U ... -- <files>` it runs `arch-update aur-handoff pacman -U ...`.
This puts the files in the stage folder and runs the shim (aur_root.py, installed
as SHIM) through sudo with no arguments. The sudoers rule names the shim and
nothing else: no line takes a path from the account.

Before the hand-over the same checks the shim makes run here, so a refusal is
explained before sudo is asked; the shim makes them again on its own copy. The
age of each package's AUR entry is read again too: yay cloned the PKGBUILD after
the run's own look, and an entry that changed in between must not get through.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from . import aur_root
from .shell import Shell
from .stores import AUR_RPC

SHIM = Path("/usr/local/libexec/arch-update-aur-install")
STAGE = Path.home() / aur_root.STAGE
REINSTALL = f"sudo install -D -o root -g root -m 0755 {Path(aur_root.__file__)} {SHIM}"
INSTALL = ["pacman", "-U", "--noconfirm", "--config", "/etc/pacman.conf", "--"]
# yay marks install reasons after an install; an update keeps the old reason, so these are answered and skipped.
REASONS = (["pacman", "-D", "-q", "--asexplicit"], ["pacman", "-D", "-q", "--asdeps"])


def handoff(argv: list[str], min_age_days: float, sh: Shell | None = None) -> int:
    sh = sh or Shell()
    if any(argv[: len(reason)] == reason for reason in REASONS):
        return 0
    if argv[: len(INSTALL)] != INSTALL or len(argv) == len(INSTALL):
        print(f"arch-update: an unattended AUR update only installs packages; refused: {' '.join(argv)[:300]}", file=sys.stderr)
        return 1
    files = [Path(path) for path in argv[len(INSTALL) :]]
    try:
        installed = aur_root.installed_foreign()
        names = [aur_root.inspect(path, installed)["name"] for path in files]
    except aur_root.Refused as refusal:
        print(f"arch-update: refused, nothing installed:\n{refusal}", file=sys.stderr)
        return 1
    answer = sh.post(AUR_RPC, [("arg[]", name) for name in names])
    if answer.get("type") != "multiinfo":
        print(f"arch-update: AUR answered {answer.get('type')}: {answer.get('error')}; nothing installed", file=sys.stderr)
        return 1
    cutoff = time.time() - min_age_days * 86400
    fresh = sorted(item["Name"] for item in answer["results"] if item["LastModified"] > cutoff)
    if fresh:
        print(f"arch-update: changed in the AUR while this run was building, nothing installed: {', '.join(fresh)}", file=sys.stderr)
        return 1
    try:
        current = SHIM.read_bytes() == Path(aur_root.__file__).read_bytes()
    except OSError:
        current = False
    if not current:
        print(f"arch-update: {SHIM} is missing or older than this release; nothing installed. Reinstall it: {REINSTALL}", file=sys.stderr)
        return 1
    STAGE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for leftover in STAGE.iterdir():
        leftover.unlink()
    try:
        for path in files:
            shutil.copyfile(path, STAGE / path.name)
        return sh.interactive(["sudo", "-n", str(SHIM)])
    finally:
        for staged in STAGE.iterdir():
            staged.unlink(missing_ok=True)


def flags(launcher: str) -> list[str]:
    """What yay is given so that it hands over instead of running sudo itself."""
    return ["--noremovemake", "--sudo", launcher, "--sudoflags", "aur-handoff"]


def shim_installed(sh: Shell) -> bool:
    return sh.root_binary(SHIM.name, dirs=(str(SHIM.parent),))[0] is not None
