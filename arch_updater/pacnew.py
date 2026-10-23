"""The .pacnew files an update left, each with its state, and the one thing done to them by itself.

`pacdiff -o` lists them. Each .pacnew is then one of:

  identical         the same bytes as the live file: nothing to merge. The run deletes it
                    through the shim (pacnew_prune.py), when that is installed: the only
                    file this tool removes on its own, and it says so in the report.
  original_missing  no live file beside it: pacman found one changed or removed, and a
                    person should look before anything else happens to it.
  different         a merge for a person, with `sudo pacdiff`, as before.
  unreadable        this account may not read one of the two (a file only uid 0 reads);
                    the shim, which can, decides.

A .pacsave is listed and never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import pacnew_prune

if TYPE_CHECKING:
    from .auto import World

SHIM = Path("/usr/local/libexec/arch-update-pacnew-prune")
REINSTALL = f"sudo install -D -o 0 -g 0 -m 0755 {Path(pacnew_prune.__file__)} {SHIM}"


def state(path: str) -> str:
    pacnew = Path(path)
    if not pacnew.name.endswith(pacnew_prune.SUFFIX):
        return "pacsave"
    live = pacnew.with_name(pacnew.name[: -len(pacnew_prune.SUFFIX)])
    if not live.exists() and not live.is_symlink():
        return "original_missing"
    try:
        with pacnew.open("rb"), live.open("rb"):
            pass
    except PermissionError:
        return "unreadable"
    except OSError:
        return "different"
    return "identical" if pacnew_prune.same_bytes(pacnew, live) else "different"


def states(paths: list[str]) -> dict[str, str]:
    return {path: state(path) for path in paths}


def shim_ready(world: World) -> str:
    """Empty when the shim may be run; otherwise why not."""
    if world.sh.root_binary(SHIM.name, dirs=(str(SHIM.parent),))[0] is None:
        return f"{SHIM} is not installed"
    try:
        current = SHIM.read_bytes() == Path(pacnew_prune.__file__).read_bytes()
    except OSError:
        current = False
    return "" if current else f"{SHIM} is older than this release; reinstall it: {REINSTALL}"


def tidy(world: World, act: bool = True) -> dict[str, Any]:
    """After the update: delete the identical .pacnew files through the shim, and say what is left in which state.

    With `act` false (a dry run) the states are read and nothing is run.
    Every deletion the shim reports is recorded, listed by pacdiff or not.
    """
    found = states(world.pacnew)
    removed: list[str] = []
    candidates = [path for path, kind in found.items() if kind in ("identical", "unreadable")]
    why = shim_ready(world) if candidates else ""
    if candidates and not why and act:
        result = world.command(["sudo", "-n", str(SHIM)])
        for line in result.output.splitlines():
            path = line.removeprefix("removed ").strip()
            if line.startswith(f"removed {pacnew_prune.ETC}/") and path.endswith(pacnew_prune.SUFFIX):
                removed.append(path)
        if result.returncode != 0:
            why = f"{SHIM} exited {result.returncode}"
    world.pacnew = [path for path in world.pacnew if path not in removed]
    return {"removed": removed, "states": {path: kind for path, kind in found.items() if path not in removed}, "not_pruned": why}


def status_lines(report: dict[str, Any]) -> list[str]:
    """What happened to each .pacnew and what a person does with the rest, one entry each, for `auto --status`."""
    kinds = report.get("pacnew_states") or {}
    lines = [f"pacnew removed (identical to the live file): {path}" for path in report.get("pacnew_removed") or []]
    for path in report.get("pacnew") or []:
        kind = kinds.get(path, "different")
        if kind == "identical":
            lines.append(f"pacnew identical to the live file, left: {path} ({report.get('pacnew_not_pruned') or 'not pruned'})\n  fix: sudo rm {path}")
        elif kind == "original_missing":
            lines.append(f"pacnew with no live file beside it, look before anything: {path}\n  fix: sudo pacdiff")
        else:
            lines.append(f"pacnew: {path}\n  fix: sudo pacdiff")
    return lines
