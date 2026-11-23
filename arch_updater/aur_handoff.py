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

A package those checks refuse is set aside, not the batch: the others go on to the
shim, and the refused one is written to LEARNED so later runs do not build it.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

from . import atomic, aur_root
from .engine import STATE_ROOT
from .shell import Shell
from .stores import AUR_RPC, BY_HAND, read_by_hand

SHIM = Path("/usr/local/libexec/arch-update-aur-install")
STAGE = Path.home() / aur_root.STAGE
REINSTALL = f"sudo install -D -o root -g root -m 0755 {Path(aur_root.__file__)} {SHIM}"
INSTALL = ["pacman", "-U", "--noconfirm", "--config", "/etc/pacman.conf", "--"]
# yay marks install reasons after an install; an update keeps the old reason, so these are answered and skipped.
REASONS = (["pacman", "-D", "-q", "--asexplicit"], ["pacman", "-D", "-q", "--asdeps"])
LEARNED = STATE_ROOT / BY_HAND
# What yay asks for to install the dependencies of a build: these flags, and package names, nothing else.
SYNC_FLAGS = {"--noconfirm", "--asdeps", "--needed"}


def repo_request(argv: list[str]) -> list[str] | None:
    """The names of `pacman -S [--asdeps] [--needed] --noconfirm --config /etc/pacman.conf -- names`, or None for any other command line."""
    if argv[:2] != ["pacman", "-S"] or "--" not in argv:
        return None
    cut = argv.index("--")
    names = argv[cut + 1 :]
    if [flag for flag in argv[2:cut] if flag not in SYNC_FLAGS] != ["--config", "/etc/pacman.conf"]:
        return None
    if not names or len(names) > aur_root.MAX_DEPS or not all(aur_root.DEP_NAME.fullmatch(name) for name in names):
        return None
    return names


def built_version(name: str, package: Path) -> str | None:
    """The version a package file was built for, read from its name (name-version-arch.pkg.tar.zst), or None."""
    if not package.name.startswith(f"{name}-"):
        return None
    rest = package.name[len(name) + 1 :]
    version = re.sub(r"-[^-]+\.pkg\.tar(\.\w+)?$", "", rest)
    return version if version and version != rest else None


def staging_problem(package: Path) -> str | None:
    """Why the shim would not take this file into its stage (its own rules in aur_root), or None."""
    if not aur_root.PACKAGE.fullmatch(package.name):
        return "its file name is not one the shim stages (letters, digits and @ . _ + - only, ending .pkg.tar.zst; an epoch colon does not fit)"
    try:
        if package.stat().st_size > aur_root.MAX_PACKAGE:
            return "the file is larger than the shim takes"
    except OSError as exc:
        return f"the file cannot be read ({exc.strerror})"
    return None


def remember(refused: dict[str, dict[str, str]]) -> None:
    """Add the refused packages to LEARNED: the shim's first reason and the version it refused."""
    known = read_by_hand(LEARNED)
    known.update(refused)
    try:
        atomic.write(LEARNED, json.dumps(known, indent=1, sort_keys=True, ensure_ascii=False))
    except OSError as exc:  # the list is a convenience: failing to write it must not stop the hand-over
        print(f"arch-update: could not write {LEARNED}: {exc.strerror}", file=sys.stderr)


def held_back(argv: list[str]) -> tuple[list[str], set[str]]:
    """The command without the --ignore list yay passes on, and the names in it.

    The run tells yay `--ignore` for the packages it holds back, and yay hands
    the same flags to `pacman -U`. For named files they change nothing; the
    shim runs its own pacman line. They are set aside here so a plain install
    is still recognised, and kept to check that no held package was built.
    Only flags before `--` count; a trailing `--ignore` with no name is left in
    and refused.
    """
    end = argv.index("--") if "--" in argv else len(argv)
    rest: list[str] = []
    names: set[str] = set()
    i = 0
    while i < end:
        arg = argv[i]
        if arg == "--ignore" and i + 1 < end:
            names.update(filter(None, argv[i + 1].split(",")))
            i += 2
            continue
        if arg.startswith("--ignore="):
            names.update(filter(None, arg.split("=", 1)[1].split(",")))
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest + argv[end:], names


def handoff(argv: list[str], min_age_days: float, sh: Shell | None = None) -> int:
    sh = sh or Shell()
    if any(argv[: len(reason)] == reason for reason in REASONS):
        return 0
    argv, ignored = held_back(argv)
    if wanted := repo_request(argv):
        print(f"arch-update: repository packages a build asks for, handed to the shim: {' '.join(wanted)}", file=sys.stderr)
        return deliver(sh, [], wanted)
    if argv[: len(INSTALL)] != INSTALL or len(argv) == len(INSTALL):
        print(f"arch-update: an unattended AUR update only installs packages; refused: {' '.join(argv)[:300]}", file=sys.stderr)
        return 1
    files = [Path(path) for path in argv[len(INSTALL) :]]
    installed = aur_root.installed_foreign()
    passed: dict[Path, str] = {}
    refused: dict[str, dict[str, str]] = {}
    for path in files:
        try:
            name = aur_root.inspect(path, installed)["name"]
            if problem := staging_problem(path):
                raise aur_root.Refused(f"{name}: {problem}")
            passed[path] = name
        except aur_root.Refused as refusal:
            print(f"arch-update: set aside for an update by hand:\n{refusal}", file=sys.stderr)
            name, _, reason = str(refusal).partition("\n")[0].partition(": ")
            if name in installed:  # a message that starts with a file name (not a readable package) names no package: set aside for this run only
                refused[name] = {"why": f"the shim refused it: {reason}"}
                if version := built_version(name, path):
                    refused[name]["version"] = version
    if refused:
        remember(refused)
    if len(passed) > aur_root.MAX_FILES:
        print(f"arch-update: the shim takes {aur_root.MAX_FILES} packages at a time; the rest wait for the next run", file=sys.stderr)
        passed = dict(list(passed.items())[: aur_root.MAX_FILES])
    files, names = list(passed), list(passed.values())
    if not names:
        print("arch-update: refused, nothing installed", file=sys.stderr)
        return 1
    if held := sorted(ignored.intersection(names)):
        print(f"arch-update: a package this run holds back was built anyway, nothing installed: {', '.join(held)}", file=sys.stderr)
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
    return deliver(sh, files, [])


def deliver(sh: Shell, files: list[Path], wanted: list[str]) -> int:
    """Stage the package files and the repository names, run the shim once, empty the stage."""
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
        if wanted:
            (STAGE / aur_root.DEPS).write_text("\n".join(wanted) + "\n", encoding="utf-8")
        return sh.interactive(["sudo", "-n", str(SHIM)])
    finally:
        for staged in STAGE.iterdir():
            staged.unlink(missing_ok=True)


def flags(launcher: str) -> list[str]:
    """What yay is given so that it hands over instead of running sudo itself."""
    return ["--noremovemake", "--sudo", launcher, "--sudoflags", "aur-handoff"]


def shim_installed(sh: Shell) -> bool:
    return sh.root_binary(SHIM.name, dirs=(str(SHIM.parent),))[0] is not None
