"""Back to the versions a record names: what changed since it, and the `pacman -U` that would undo it from the cache.

Every update writes `packages.txt` (`pacman -Q`) into its record before the
first change (auto.World._keep). This compares that list with the machine now,
finds each old version's package file in pacman's cache (and yay's, for AUR
packages), and prints the plan. Nothing is installed unless a person says yes:
at a terminal it asks, `--yes` skips that question, and without a terminal it
only prints. pacman still asks its own questions (a conflict, a replacement),
so the install runs on the terminal, never with --noconfirm.

What it cannot undo: a version no cache holds any more (listed and skipped),
and a package installed since (listed with the command that removes it).
Rolling back part of an update can leave packages that expect each other's new
versions; the plan says so when anything is skipped.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .engine import aur_cache_root
from .shell import Shell

CACHE = "/var/cache/pacman/pkg"
RECORD = re.compile(r"auto-\d{8}T\d{6}[+-]\d{4}(-\d+)?")


@dataclass
class Step:
    name: str
    now: str | None  # None: removed since the record
    was: str
    file: str | None  # the cached package of the old version, None when no cache has it


@dataclass
class Plan:
    record: str
    steps: list[Step] = field(default_factory=list)
    added: list[str] = field(default_factory=list)  # installed since the record: not undone here

    @property
    def files(self) -> list[str]:
        return [step.file for step in self.steps if step.file]

    @property
    def missing(self) -> list[Step]:
        return [step for step in self.steps if not step.file]


def listing(text: str) -> dict[str, str]:
    return dict(line.split()[:2] for line in text.splitlines() if len(line.split()) >= 2)


def records(keep: Path) -> list[Path]:
    """The records under `keep` that hold a package list, oldest first."""
    if not keep.is_dir():
        return []
    return sorted(path for path in keep.iterdir() if RECORD.fullmatch(path.name) and (path / "packages.txt").is_file() and not path.is_symlink())


def caches(sh: Shell) -> list[Path]:
    answer = sh.capture(["pacman-conf", "CacheDir"], timeout=10)
    found = [Path(line.strip()) for line in answer.output.splitlines() if line.strip().startswith("/")] if answer.returncode == 0 else []
    return [*(found or [Path(CACHE)]), aur_cache_root()]


def cached(name: str, version: str, folders: list[Path]) -> str | None:
    """The package file of exactly this name and version: pacman's cache, or yay's folder for the package."""
    wanted = re.compile(re.escape(name) + "-" + re.escape(version) + r"-[A-Za-z0-9_]+\.pkg\.tar(?:\.[a-z0-9]+)?")
    for folder in folders:
        for place in (folder, folder / name):
            if not place.is_dir():
                continue
            for path in sorted(place.iterdir()):
                if wanted.fullmatch(path.name) and path.is_file():
                    return str(path)
    return None


def plan(sh: Shell, record: Path) -> Plan:
    before = listing((record / "packages.txt").read_text(encoding="utf-8"))
    now_listing = sh.capture(["pacman", "-Q"], timeout=60)
    if now_listing.returncode != 0:
        raise OSError(f"pacman -Q failed: {now_listing.output.strip()[-200:]}")
    now = listing(now_listing.output)
    folders = caches(sh)
    found = Plan(record.name)
    for name, was in sorted(before.items()):
        if now.get(name) != was:
            found.steps.append(Step(name, now.get(name), was, cached(name, was, folders)))
    found.added = sorted(set(now) - set(before))
    return found


def describe(found: Plan) -> str:
    if not found.steps and not found.added:
        return f"nothing changed since the record {found.record}\n"
    width = max([len(step.name) for step in found.steps + [Step(name, None, "", None) for name in found.added]] + [0]) + 2
    lines = [f"record {found.record}: {len(found.steps)} package{'s' if len(found.steps) != 1 else ''} to put back"]
    for step in found.steps:
        change = f"{step.now} → back to {step.was}" if step.now else f"removed since, back to {step.was}"
        lines.append(f"  {step.name:<{width}}{change}  {step.file or 'not in the cache: skipped'}")
    lines += [f"  {name:<{width}}installed since; not undone (by hand: sudo pacman -Rs {name})" for name in found.added]
    if found.missing and found.files:
        lines.append(f"{len(found.missing)} cannot be put back from the cache; putting back only the rest can leave packages that expect each other's new versions")
    if found.files:
        lines.append("to roll back: " + " ".join(command(found)))
    return "\n".join(lines) + "\n"


def command(found: Plan) -> list[str]:
    return ["sudo", "pacman", "-U", "--", *found.files]


def run(
    sh: Shell, keep: Path, record: str | None = None, yes: bool = False, stdin: TextIO = sys.stdin, out: TextIO = sys.stdout
) -> int:
    """Show the plan; install it only on a yes at a terminal (or --yes there). 0 when nothing was asked to change."""
    known = records(keep)
    chosen = next((path for path in known if path.name == record), None) if record else (known[-1] if known else None)
    if chosen is None:
        named = f"no record {record}" if record else "no record yet"
        raise ValueError(f"{named} in {keep}; records: {', '.join(path.name for path in known) or 'none'}")
    found = plan(sh, chosen)
    out.write(describe(found))
    if not found.files:
        return 0
    terminal = stdin.isatty() and out.isatty()
    if not terminal:
        if yes:
            out.write("pacman asks its own questions while it installs: run this at a terminal\n")
            return 1
        return 0
    if not yes:
        from .day import confirm  # the screen's own yes/no, read on the same terminal

        if not confirm(f"Roll back {len(found.files)} package{'s' if len(found.files) != 1 else ''} with pacman -U?", stdin, out):
            out.write("nothing was changed\n")
            return 0
    return sh.interactive(command(found))


def as_dict(found: Plan) -> dict[str, Any]:
    return {"record": found.record, "steps": [step.__dict__ for step in found.steps], "added": found.added, "command": command(found) if found.files else None}
