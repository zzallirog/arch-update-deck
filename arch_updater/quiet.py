"""The unattended run, quiet: it starts only when nobody is bothered, and runs under a ceiling, not a priority.

A lower priority only orders the queue; the machine's total load is what raises
watts and spins fans, and a background build at low priority still fills every
core. A ceiling does not: the run's own cgroup gets a few distinct physical
cores (AllowedCPUs, a cpuset, which holds under any scheduler, sched_ext
included) and a quota on top (CPUQuota, which only the kernel's fair scheduler
enforces). Everything the run starts inherits it: sudo, pacman, its hooks, a
build. systemd keeps a runtime property until the next boot, not until the unit
stops, so the run lifts its own ceiling when it ends (release) and lifts any
left from an earlier run before it sets one, which is also why the cores are
counted from the machine, not from what this process is allowed now.

Before anything, the gate: not during a game, not while the machine is already
under pressure, not on battery. Waiting is not a failure: the run is skipped and
the timer asks again the next day, for at most MAX_WAIT_DAYS; after that the
run goes anyway and says why. Whatever cannot be read, and any setting that
makes no sense, counts as "go": a gate that cannot see must not stop the updates.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

SYS = Path("/sys")
PROC = Path("/proc")
# A game owns the machine while one of these runs.
GAMES = re.compile(r"^(gamescope|gamescope-wl|wine64-preloader|wineserver)$|^steam_app_\d+$")
PRESSURE = {"cpu": 40.0, "memory": 10.0, "io": 30.0}  # "some avg60" above this: the machine is busy already
SHARE = 4  # a quarter of the physical cores, at least one
QUOTA_PER_CORE = 60  # % of one CPU for each core given: below a full core, so the cores never sit at the top clock
MAX_WAIT_DAYS = 3
WAITING = "quiet-waiting.json"  # in the state directory: since when the run has been waiting
CEILING = ("AllowedCPUs", "CPUQuota", "CPUWeight", "IOWeight")


def gate(config: dict[str, Any] | None, sys_root: Path | None = None, proc: Path | None = None) -> str | None:
    """Why the run should wait now, or None. Off with "quiet": false in auto.json. Never raises."""
    try:
        if (config or {}).get("quiet") is False:
            return None
        sys_root, proc = sys_root or SYS, proc or PROC  # read when called: a test moves them
        game = _game(proc)
        if game:
            return f"a game is running ({game})"
        limits = dict(PRESSURE)
        custom = (config or {}).get("quiet_pressure")
        if isinstance(custom, dict):
            limits.update({kind: value for kind, value in custom.items() if isinstance(value, (int, float)) and not isinstance(value, bool)})
        for kind, limit in limits.items():
            level = _pressure(proc, kind)
            if level is not None and level > float(limit):
                return f"{kind} pressure {level:.0f}% over the last minute (limit {limit})"
        if _on_battery(sys_root):
            return "on battery"
    except Exception:  # noqa: BLE001 - a gate that breaks must let the update through, not stop it every day
        return None
    return None


def waited(reason: str | None, state_root: Path, now: float | None = None) -> str | None:
    """The gate's answer, bounded: after MAX_WAIT_DAYS of waiting the run goes anyway. Keeps the date it started waiting."""
    path = state_root / WAITING
    now = now or time.time()
    try:
        if reason is None:
            path.unlink(missing_ok=True)
            return None
        try:
            since = float(json.loads(path.read_text(encoding="utf-8"))["since"])
        except (OSError, ValueError, KeyError, TypeError):
            since = now
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"since": since, "reason": reason}), encoding="utf-8")
        if now - since >= MAX_WAIT_DAYS * 86400:
            path.unlink(missing_ok=True)
            return None
    except OSError:
        return None
    return reason


def _game(proc: Path) -> str:
    try:
        entries = list(proc.iterdir())
    except OSError:
        return ""
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if GAMES.search(name):
            return name
    return ""


def _pressure(proc: Path, kind: str) -> float | None:
    try:
        text = (proc / "pressure" / kind).read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"^some .*?avg60=([\d.]+)", text, re.MULTILINE)
    return float(found[1]) if found else None


def _on_battery(sys_root: Path) -> bool:
    """True only when a mains supply exists and says it is offline: a desktop has none and is never on battery."""
    mains = []
    for supply in (sys_root / "class" / "power_supply").glob("*"):
        try:
            if (supply / "type").read_text(encoding="utf-8").strip() == "Mains":
                mains.append((supply / "online").read_text(encoding="utf-8").strip() == "1")
        except OSError:
            continue
    return bool(mains) and not any(mains)


def _online(sys_root: Path) -> set[int]:
    """The CPUs the machine has online: a ceiling left from an earlier run must not shrink the next one."""
    try:
        text = (sys_root / "devices/system/cpu/online").read_text(encoding="utf-8").strip()
    except OSError:
        return set(os.sched_getaffinity(0))
    found: set[int] = set()
    for part in text.split(","):
        low, _, high = part.partition("-")
        found.update(range(int(low), int(high or low) + 1))
    return found


def cores(allowed: set[int] | None = None, sys_root: Path | None = None, share: int = SHARE) -> list[int]:
    """A `share`-th of the physical cores, one thread of each: SMT siblings add little to a build."""
    sys_root = sys_root or SYS
    allowed = allowed if allowed is not None else _online(sys_root)
    share = share if isinstance(share, int) and not isinstance(share, bool) and share >= 1 else SHARE
    physical: dict[str, int] = {}
    for cpu in sorted(allowed):
        try:
            siblings = (sys_root / "devices/system/cpu" / f"cpu{cpu}" / "topology/thread_siblings_list").read_text(encoding="utf-8").strip()
        except OSError:
            siblings = str(cpu)
        physical.setdefault(siblings, cpu)
    first = sorted(physical.values())
    return first[: max(1, len(first) // share)]


def own_unit(proc: Path | None = None) -> str | None:
    """The systemd user unit this process runs in, if it is the update's own service."""
    try:
        line = ((proc or PROC) / "self" / "cgroup").read_text(encoding="utf-8").strip().splitlines()[-1]
    except (OSError, IndexError):
        return None
    unit = line.rsplit("/", 1)[-1]
    return unit if unit.startswith("arch-update") and unit.endswith(".service") else None


def _runner(run: Any) -> Any:
    return run or (lambda argv: subprocess.run(argv, capture_output=True, timeout=10, check=False).returncode)


def release(run: Any = None, proc: Path | None = None) -> bool:
    """Lift the ceiling from the update's own service: what systemd would otherwise keep until the next boot."""
    unit = own_unit(proc)
    if unit is None:
        return False
    try:
        return _runner(run)(["systemctl", "--user", "set-property", "--runtime", unit, *(f"{name}=" for name in CEILING)]) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def calm(config: dict[str, Any] | None, run: Any = None, proc: Path | None = None, sys_root: Path | None = None) -> list[str]:
    """Put the ceiling on the running service and lower its priority. Returns what was done, one line each.

    Only inside the update's own service: a run by hand has a person waiting for it. With "quiet": false a
    ceiling left from before is lifted and nothing is set. Never raises.
    """
    unit = own_unit(proc)
    if unit is None:
        return []
    run = _runner(run)
    release(run, proc)  # whatever an earlier run left, before anything is counted
    if (config or {}).get("quiet") is False:
        return []
    done = []
    try:
        picked = cores(sys_root=sys_root, share=(config or {}).get("quiet_share", SHARE))
        properties = [f"AllowedCPUs={','.join(map(str, picked))}", f"CPUQuota={QUOTA_PER_CORE * len(picked)}%", "CPUWeight=20", "IOWeight=20"]
        if run(["systemctl", "--user", "set-property", "--runtime", unit, *properties]) == 0:
            done.append(f"{unit}: {' '.join(properties)}")
        if run(["ionice", "-c", "3", "-p", str(os.getpid())]) == 0:
            done.append("io idle")
        os.nice(10)
        done.append("nice 10")
    except Exception:  # noqa: BLE001 - a ceiling that cannot be set must not stop the update
        pass
    return done
