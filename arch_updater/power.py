"""The machine must stay up while packages or boot files change.

A power-off, a suspend, a lid closed on a laptop or an empty battery in the middle of a transaction can leave a
half-installed system. Two things are done about it, for every transaction this tool starts (the unattended run,
Update Day, `arch-update kernel`, `arch-update rollback`, `arch-update learn`, `arch-update dotfiles`):

* a block inhibitor in logind (shutdown, sleep, idle, handle-lid-switch) is held from before the first
  transaction until the whole phase ends. It is one helper process, `systemd-inhibit ... sh -c 'read'`, in a
  session of its own, so a Ctrl-C on the terminal does not drop it while pacman is let finish. Its input is a pipe
  from this process: if this process dies in any way, the pipe closes and the helper ends with it. Logind's lock
  belongs to the machine, not to a process tree, so it covers the root processes sudo starts as well. Wrapping each
  command in `systemd-inhibit` instead would change every command line (the sudoers rules name them exactly) and
  would leave a gap between two commands of one phase. The lock covers the whole phase, whatever it runs: nothing
  guesses from a command line whether it is a transaction (an installer, a saved fix and a `sh -c` text hide it).
* a transaction is not started on a laptop that runs on its battery below a threshold (`min_battery` in auto.json,
  30 by default). A desktop has no battery and is never stopped.

Without a working inhibitor the run goes on only when the machine is not on battery, and says so.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

from . import quiet

WHAT = "shutdown:sleep:idle:handle-lid-switch"
WHO = "arch-update"
MIN_BATTERY = 30  # percent; below it, on battery, no transaction is started
ANSWER_WAIT = 10.0  # seconds the helper has to say it holds the lock


class Refused(RuntimeError):
    """A transaction that must not start now. `case` is its entry in the catalogue of known failures."""

    def __init__(self, case: str, reason: str) -> None:
        super().__init__(f"{case}: {reason}")
        self.case, self.reason = case, reason


class Battery(NamedTuple):
    present: bool  # the machine has a battery of its own (a mouse's or a phone's is not one)
    on_battery: bool  # it is running on it
    percent: int | None  # the lowest charge among the batteries in use, None when it cannot be read


def threshold(config: dict[str, Any] | None) -> int:
    """`min_battery` of auto.json, 0 to 100; anything else is the default (a refusal is the safe side)."""
    value = (config or {}).get("min_battery", MIN_BATTERY)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100 else MIN_BATTERY


def config_of_machine() -> dict[str, Any] | None:
    """auto.json as the run reads it, for the entry points that are not given the configuration."""
    from .auto import CONFIG_PATH  # late: auto imports the modules that import this one

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def battery(sys_root: Path | None = None) -> Battery:
    """Read /sys/class/power_supply the way quiet.py does: a supply of type Battery and scope System is the machine's own."""
    sys_root = sys_root or quiet.SYS  # read when called: a test moves it
    charges: dict[str, tuple[bool, int | None]] = {}
    for supply in sorted((sys_root / "class" / "power_supply").glob("*")):
        try:
            if (supply / "type").read_text(encoding="utf-8").strip() != "Battery":
                continue
            with contextlib.suppress(OSError):
                if (supply / "scope").read_text(encoding="utf-8").strip() == "Device":
                    continue  # a mouse, a headset, a phone
            status = (supply / "status").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            percent: int | None = int((supply / "capacity").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            percent = None
        charges[supply.name] = (status == "Discharging", percent)
    if not charges:
        return Battery(False, False, None)
    on_battery = any(discharging for discharging, _ in charges.values()) or quiet._on_battery(sys_root)  # noqa: SLF001
    pool = [percent for discharging, percent in charges.values() if discharging and percent is not None]
    pool = pool or [percent for _, percent in charges.values() if percent is not None]
    return Battery(True, on_battery, min(pool) if pool else None)


def on_battery(sys_root: Path | None = None) -> bool:
    try:
        return battery(sys_root).on_battery
    except Exception:  # noqa: BLE001 - what cannot be read is not "on battery": a desktop must never be stopped
        return False


def low(config: dict[str, Any] | None, sys_root: Path | None = None) -> str | None:
    """Why no transaction may start now (a discharging battery under the threshold), or None. Never raises."""
    try:
        state = battery(sys_root)
        limit = threshold(config)
        if state.on_battery and state.percent is not None and state.percent < limit:
            return f"the battery is at {state.percent}% and discharging, under the {limit}% a package update needs to start; plug in the charger"
    except Exception:  # noqa: BLE001 - a reading that breaks must not stop a desktop
        return None
    return None


class Inhibitor:
    """The lock, or the reason there is none. `held` is true while the helper that holds it runs."""

    def __init__(self, process: subprocess.Popen[str] | None = None, error: str = "") -> None:
        self.process, self.error = process, error

    @property
    def held(self) -> bool:
        alive = self.process is not None and self.process.poll() is None
        if self.process is not None and not alive and not self.error:
            self.error = "the process that held the lock ended"
        return alive

    def release(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if process.stdin:
                process.stdin.close()  # EOF: the helper's `read` returns and it ends
            process.wait(timeout=5)
        if process.poll() is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream:
                with contextlib.suppress(OSError):
                    stream.close()


def _says_held(process: subprocess.Popen[str], wait: float) -> bool:
    """Did the helper print `held` within `wait` seconds? Reads what is there, never waits for a newline that may not come."""
    fd = process.stdout.fileno()  # type: ignore[union-attr]
    deadline, seen = time.monotonic() + wait, b""
    while b"\n" not in seen:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return False
        chunk = os.read(fd, 256)
        if not chunk:  # the helper ended without saying it
            return False
        seen += chunk
    return seen.split(b"\n")[0].strip() == b"held"


def start(why: str, wait: float = ANSWER_WAIT) -> Inhibitor:
    """Take the lock. Never raises: a lock that cannot be taken is an Inhibitor with `held` false and the reason in `error`."""
    tool = shutil.which("systemd-inhibit")
    if tool is None:
        return Inhibitor(error="systemd-inhibit is not installed")
    argv = [tool, f"--what={WHAT}", f"--who={WHO}", f"--why={why}", "--mode=block", "sh", "-c", "echo held; read _"]
    try:
        # A session of its own: the terminal's Ctrl-C goes to the foreground group, and a transaction is let finish.
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as exc:
        return Inhibitor(error=f"systemd-inhibit did not start ({exc})")
    assert process.stdout is not None and process.stderr is not None
    answered = _says_held(process, wait)
    if answered:
        return Inhibitor(process)
    if process.poll() is None:
        process.kill()
    said = ""
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        process.wait(timeout=5)
        said = process.stderr.read()
    Inhibitor(process).release()  # closes the pipes
    return Inhibitor(error=f"systemd-inhibit could not take the lock ({said.strip()[-200:] or 'no answer'})")


class _Phase:
    def __init__(self, why: str, lock: Inhibitor, note: Callable[[str], None] | None) -> None:
        self.why, self.lock, self.note = why, lock, note


_phases: list[_Phase] = []  # the guards open now, innermost last


def checkpoint(strict: bool = True) -> None:
    """Inside a guard, before the next step: a lock whose helper died is taken again.

    Without a lock on battery a strict checkpoint raises Refused, so the step is not started; a lax one (the middle of a
    step that must be finished, GRUB's) only says so. On mains the phase goes on with a note. Outside a guard: nothing.
    """
    if not _phases:
        return
    phase = _phases[-1]
    if phase.lock.held:
        return
    phase.lock.release()
    phase.lock = start(phase.why)
    if phase.lock.held:
        return
    if on_battery() and strict:
        raise Refused("NO-INHIBITOR", f"the shutdown lock was lost ({phase.lock.error}) and the machine runs on its battery; the next step was not started")
    (phase.note or _say)(no_lock_on_mains(phase.lock.error))


@contextlib.contextmanager
def guard(why: str, config: dict[str, Any] | None = None, note: Callable[[str], None] | None = None) -> Iterator[Inhibitor]:
    """Around a whole transaction phase that is not the unattended run: refuse on a low battery, hold the lock.

    Raises Refused when the battery is low, or when there is no lock and the machine is on battery. Without a lock
    on mains the phase goes on and `note` is told why there is none. `checkpoint()` inside it takes a lost lock again.
    """
    config = config_of_machine() if config is None else config
    if reason := low(config):
        raise Refused("LOW-BATTERY", reason)
    lock = start(why)
    if not lock.held:
        if on_battery():
            raise Refused("NO-INHIBITOR", no_lock_on_battery(lock.error))
        (note or _say)(no_lock_on_mains(lock.error))
    phase = _Phase(why, lock, note)
    _phases.append(phase)
    try:
        yield lock
    finally:
        _phases.remove(phase)
        phase.lock.release()


def no_lock_on_battery(error: str) -> str:
    return f"{error}, and the machine runs on its battery: nothing was started; plug in the charger or fix systemd-inhibit"


def no_lock_on_mains(error: str) -> str:
    return f"no shutdown or sleep lock was taken ({error}); the machine is on mains power, so the update goes on without it"


def _say(message: str) -> None:
    print(f"arch-update: {message}", file=sys.stderr)

