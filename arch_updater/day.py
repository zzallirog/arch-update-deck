"""Update day: the one screen of the program.

A dnf-style transaction in plain words: what will change and where it comes
from, whether the machine is in order, what still hangs from last time and the
command that fixes it, one question. Nothing on it needs explaining to someone
who has never heard of the engine. `arch-update` opens it; the weekly timer
opens it too when its owner asked to be asked.

Plain ANSI in black, grey and white, with red for what is wrong. One write per
frame inside a synchronized update, so a GPU terminal paints it whole. The
update queue is read here only to be shown.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TextIO

from . import auto, exits, quiet
from .engine import STATE_ROOT, available_kernel_profiles, load_json, read_aur_queue, select_kernel
from .keepalive import NOTICE as TICKET_EXPIRED, Keepalive
from .scheduler import AUTO_UNIT
from .shell import Shell
from .textsafe import cause, clean

WHITE, GREY, DIM, RED = (230, 230, 230), (128, 128, 128), (64, 64, 64), (208, 49, 45)
FRAME_START, FRAME_END = "\x1b[?2026h\x1b[H", "\x1b[J\x1b[?2026l"  # synchronized update: the frame lands whole
MAX_COLUMNS = 124
WINDOW = {
    "kitty": [
        "--title=update day",
        "--override=initial_window_width=118c",
        "--override=initial_window_height=36c",
        "--override=background=#000000",
        "--override=window_padding_width=10",
    ]
}
_QUEUE_LINE = re.compile(r"^(\S+)\s+(\S+)\s+->\s+(\S+)")
_SNAP_LINE = re.compile(r"^(\S+)\s+(\S+)\s+\d+\s")  # name, version, revision
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NEEDS_PASSWORD = ("NO-ROOT", "SUDO-TIMESTAMP-EXPIRED")  # what a person at the terminal settles by typing the password
STORE_NAMES = {"repo": "repositories", "aur": "AUR", "flatpak": "flatpak", "snap": "snap"}
HOLD_ENV = "ARCH_UPDATE_HOLD"  # set by the .desktop entry: its window closes with the program, so the screen waits for Enter
ASK_WAIT_MINUTES = 30  # how long the question waits for an answer before the week is skipped
ASK_RESULT = "ask-result.json"
ASK_LOG = "ask-window.log"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LABEL = 9  # width of MACHINE, PENDING and TOTAL with the gap after them
KEYS = "l last run · k kernel"
WEEKLY_OFF = "weekly update is off: arch-update schedule --install-auto"
LOOK_ONLY = "look only: run arch-update in a terminal to update"
BY_HAND_ONLY = f"nothing for the update to do; by hand: {auto.AUR_BY_HAND}"
# What each check is called on screen; a condition from the config keeps the name its owner gave it.
CHECKS = {
    "privilege": "root access",
    "conditions": "config",
    "keep": "backup record",
    "lock": "pacman is busy",
    "disk": "disk space",
    "dns": "network",
    "database": "package database",
    "machine": "kernel and services",
    "smoke": "programs",
}


def unread_prompt(has_checkupdates: bool) -> str:
    """Under a repository queue that could not be read nothing is asked: yes would update blind."""
    return f"nothing is asked while the queue cannot be read; by hand: {'checkupdates' if has_checkupdates else 'sudo pacman -S pacman-contrib'}"


def human(probe: str) -> str:
    return CHECKS.get(probe) or STORE_NAMES.get(probe, probe)


def _stop(signum: int, frame: Any) -> None:
    raise exits.Stopped(signum)


def interruption(started: bool, child_stopped: bool | None) -> str:
    """What to tell after a signal ended the screen. 'Stopped' only where a command is known to have ended by a signal."""
    if not started:
        return "interrupted; nothing was changed"
    if child_stopped:
        return "interrupted; the update was stopped, run arch-update again"
    return "interrupted; the update may have finished — run arch-update to check"


def now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Row:
    name: str
    old: str
    new: str
    store: str

    def __post_init__(self) -> None:
        # Written by whoever packaged it: no escape reaches the screen, and a cell is one line of one word.
        for field in ("name", "old", "new"):
            object.__setattr__(self, field, " ".join(clean(getattr(self, field)).split()))


def _cells(text: str) -> list[list[str]]:
    return [[cell.strip() for cell in line.split("\t")] for line in text.splitlines() if line.strip()]


def _flatpak(sh: Shell) -> list[Row] | None:
    waiting = sh.capture(["flatpak", "remote-ls", "--updates", "--columns=application,version"], timeout=180)
    if waiting.returncode != 0:
        return None
    have = {cells[0]: cells[1] for cells in _cells(sh.capture(["flatpak", "list", "--columns=application,version"], timeout=60).output) if len(cells) > 1}
    return [Row(cells[0], have.get(cells[0]) or "?", (cells[1:] or [""])[0] or "?", "flatpak") for cells in _cells(waiting.output)]


def _snap(sh: Shell) -> list[Row] | None:
    waiting = sh.capture(["snap", "refresh", "--list"], timeout=60)
    if waiting.returncode != 0:
        return None
    have = {m[1]: m[2] for m in map(_SNAP_LINE.match, sh.capture(["snap", "list"], timeout=60).output.splitlines()) if m}
    return [Row(m[1], have.get(m[1], "?"), m[2], "snap") for m in map(_SNAP_LINE.match, waiting.output.splitlines()) if m]


def queue(sh: Shell) -> tuple[list[Row], list[str]]:
    """What is waiting, for display only, and the stores whose queue could not be read.

    The repository queue needs checkupdates (pacman-contrib); without it the queue is unread, not empty.
    The AUR queue comes from the first helper installed, the same way `status` and the patrol read it.
    """
    rows: list[Row] = []
    unread: list[str] = []

    def take(store: str, result: Any, quiet: tuple[int, ...]) -> None:
        if result.returncode not in quiet:
            unread.append(store)
        else:
            rows.extend(Row(m[1], m[2], m[3], store) for m in map(_QUEUE_LINE.match, result.output.splitlines()) if m)

    if sh.has("checkupdates"):
        take("repo", sh.capture(["checkupdates"], timeout=180), (0, 2))
    else:
        unread.append("repo")
    helper, result = read_aur_queue(sh.has, sh.capture)
    if helper:
        take("aur", result, (0, 1))
    for store, read in (("flatpak", _flatpak), ("snap", _snap)):
        if sh.has(store):
            found = read(sh)
            unread.append(store) if found is None else rows.extend(found)
    return rows, unread


def weekly_off(sh: Shell) -> bool:
    """True when the weekly timer is known not to be installed; unknown (no systemd user manager) is not off."""
    if not sh.has("systemctl"):
        return False
    shown = sh.capture(["systemctl", "--user", "show", "--property=LoadState", "--value", f"{AUTO_UNIT}.timer"], timeout=10)
    return shown.returncode == 0 and shown.output.strip() != "loaded"


PLAIN = False  # set for the length of a visit whose output is not a terminal: a pipe gets words, not colour codes


def dumb() -> bool:
    return os.environ.get("TERM") == "dumb"


def colour() -> bool:
    """Colour only on a terminal that shows it: NO_COLOR, TERM=dumb and a pipe all get plain text."""
    return not (os.environ.get("NO_COLOR") or dumb() or PLAIN)


def escapes(out: TextIO) -> bool:
    """Cursor moves, screen clears and synchronized updates only for a terminal that understands them."""
    return out.isatty() and not dumb()


def ink(rgb: tuple[int, int, int], text: str, *, on: tuple[int, int, int] | None = None) -> str:
    """The text in a colour. Every coloured or plain word on the page comes through here: it is the one place that cleans it."""
    text = clean(text)
    if not colour() or not text:
        return text
    back = f";48;2;{on[0]};{on[1]};{on[2]}" if on else ""
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}{back}m{text}\x1b[0m"


def visible(text: str) -> int:
    return len(_ANSI.sub("", text))


def fit(text: str, width: int) -> str:
    """Pad or cut plain text to exactly `width` columns."""
    return text[: width - 1] + "…" if len(text) > width else text.ljust(width)


def clip(text: str, width: int) -> str:
    """Cut a line that carries colour codes to `width` visible columns."""
    out, used = [], 0
    for part in re.split(r"(\x1b\[[0-9;?]*[A-Za-z])", text):
        if _ANSI.fullmatch(part):
            out.append(part)
        elif used + len(part) > width:
            out.append(part[: max(0, width - used - 1)] + "…" + ("\x1b[0m" if colour() else ""))
            used = width
        else:
            out.append(part)
            used += len(part)
    return "".join(out)


def block(label: str, lines: list[str]) -> list[str]:
    """A grey label beside its first line; the lines after it stand under the first."""
    return [(ink(GREY, label.ljust(LABEL)) if index == 0 else " " * LABEL) + line for index, line in enumerate(lines)]


class Screen:
    """Everything on the page. Events change it; frame() draws what it holds."""

    def __init__(self, size: tuple[int, int]) -> None:
        self.resize(size)
        self.rows: list[Row] | None = None  # None: still reading
        self.unread: list[str] = []
        self.unread_reason = "install pacman-contrib"
        self.verdicts: list[dict[str, Any]] | None = None
        self.password_later = False  # sudo will ask for the password when the owner says yes
        self.no_sudo = False  # there is no sudo to ask: yes would end with 70
        self.aur_on = False
        self.hanging: list[str] = []
        self.notes: list[str] = []  # dim lines under PENDING
        self.log: list[str] = []  # what the run has done so far; replaces the table once it starts
        self.prompt = ""
        self.keys = False  # the dim line that lists l and k
        self.lock = threading.RLock()

    def resize(self, size: tuple[int, int]) -> None:
        self.columns, self.lines = max(20, min(size[0], MAX_COLUMNS)), size[1]

    # ── events ──────────────────────────────────────────────────────────────

    def event(self, kind: str, details: dict[str, Any]) -> None:
        if kind == "look":
            self.verdicts = details["verdicts"]
            bad = [v for v in self.verdicts if v["status"] != "pass"]
            text = ink(WHITE, "machine is fine") if not bad else ink(RED, "; ".join(f"{human(v['probe'])}: {v['detail']}" for v in bad))
            self.log.append(f"{ink(GREY, 'checking   ')}{text}")
        elif kind == "store" and details["phase"] == "start":
            self.log.append(f"{ink(GREY, 'updating   ')}{ink(WHITE, STORE_NAMES.get(details['name'], details['name']))} …")
        elif kind == "store":
            mark = ink(WHITE, f"{details['changed']} changed") if details["ok"] else ink(RED, f"failed · {details['changed']} changed")
            self.log[-1] = self.log[-1].removesuffix(" …") + f"  {mark}"
        elif kind == "census":
            answer = "seen before, repairing the same way" if details["attempted"] else "cannot repair this myself; the command is below"
            self.log.append(f"{ink(GREY, 'failure    ')}{ink(RED, answer)}  {ink(DIM, str(details['case']))}")
        elif kind == "keep":
            self.log.append(f"{ink(GREY, 'record     ')}{ink(GREY, details['path'])}")

    # ── drawing ─────────────────────────────────────────────────────────────

    def _table(self, rows: list[Row], width: int, room: int | None) -> list[str]:
        """The packages, in as many columns as fit: INSTALLED goes first, then AVAILABLE. Never scrolls.

        `room` is the most lines the table may take, header and the "+ N more" line included; at least one row stays.
        """
        source = [row.store.upper() + ("" if row.store != "aur" or self.aur_on else " · BY HAND") for row in rows]
        gap = lambda head, values: max(len(head), *map(len, values)) + 3  # noqa: E731
        columns = [
            ("PACKAGE", min(36, gap("PACKAGE", [r.name for r in rows])), [r.name for r in rows], WHITE),
            ("INSTALLED", min(24, gap("INSTALLED", [r.old for r in rows])), [r.old for r in rows], GREY),
            ("AVAILABLE", min(24, gap("AVAILABLE", [r.new for r in rows])), [r.new for r in rows], WHITE),
        ]
        last = max(len("FROM"), *map(len, source))
        for dropped in ("", "INSTALLED", "AVAILABLE"):
            columns = [column for column in columns if column[0] != dropped]
            if sum(column[1] for column in columns) + last <= width:
                break
        else:
            columns[0] = (columns[0][0], max(8, width - last), *columns[0][2:])
        cell = lambda text, size, rgb: ink(rgb, fit(text, size - 1) + " ")  # noqa: E731
        head = "".join(cell(name, size, GREY) for name, size, *_ in columns) + ink(GREY, "FROM")
        shown = rows if room is None or len(rows) + 1 <= room else rows[: max(1, room - 2)]
        lines = [head]
        for index, row in enumerate(shown):
            lines.append("".join(cell(values[index], size, rgb) for _, size, values, rgb in columns) + ink(GREY, source[index]))
        if len(shown) < len(rows):
            lines.append(ink(DIM, f"+ {len(rows) - len(shown)} more"))
        return lines

    def _total(self) -> list[str]:
        rows = self.rows or []
        counts = [(store, sum(row.store == store for row in rows)) for store in STORE_NAMES]
        text = " · ".join(f"{count} from {STORE_NAMES[store]}" for store, count in counts if count)
        if self.aur_on is False and any(row.store == "aur" for row in rows):
            text += f" (by hand: {auto.AUR_BY_HAND})"
        bad = [
            ink(RED, f"cannot read the repositories: {self.unread_reason}" if store == "repo" else f"cannot read the {STORE_NAMES[store]} queue")
            for store in self.unread
        ]
        return block("TOTAL", [ink(WHITE, text), *bad] if rows else bad or [ink(WHITE, "nothing waiting")])

    def _status(self, notes: bool = True) -> list[str]:
        bad = [v for v in self.verdicts or [] if v["status"] != "pass"]
        if self.verdicts is None:
            machine = [ink(GREY, "checking …")]
        elif bad:
            machine = [ink(RED, f"{human(v['probe'])}: {' '.join(v['detail'].split())}") for v in bad]
        elif self.no_sudo:
            machine = [ink(RED, "sudo is not installed: install it, y cannot continue")]
        else:
            machine = [ink(WHITE, "fine") + ink(GREY, f" · {len(self.verdicts)} checks") + (ink(DIM, " · sudo asks for your password on yes") if self.password_later else "")]
        hanging = [ink(RED, line) for line in self.hanging] or [ink(GREY, "nothing since last time")]
        return [*block("MACHINE", machine), *block("PENDING", hanging), *(ink(DIM, line) for line in self.notes if notes)]

    def _page(self, head: list[str], *, keys: bool = True, gaps: bool = True, notes: bool = True, room: int | None = None) -> list[str]:
        """The lines of the page. `room` is what the body (the table, or the tail of the story) may take; None: all of it."""
        gap = [""] if gaps else []
        status = [] if self.log else [*gap, *self._status(notes)]
        if self.log:
            body = self.log if room is None else self.log[len(self.log) - max(0, min(room, len(self.log))) :]
        elif self.rows is None:
            body = [ink(GREY, "reading the queue …")]
        elif self.rows:
            body = [*self._table(self.rows, self.columns - 1, room), *gap, *self._total()]
        else:
            body = self._total()
        return [*head, *gap, *body, *status, *([*gap, self.prompt] if self.prompt else []), *([*gap, ink(DIM, KEYS)] if keys and self.keys else [])]

    def lines_of_page(self) -> list[str]:
        """The page cut to the screen's height. It never has more lines than the screen has rows, one being the cursor's.

        When it is too tall, what goes is the least needed, in this order: the keys footer, the blank lines between
        blocks, the notes under PENDING, then the table (or the story) down to one row and "+ N more", and last of all
        the status: what is left is the header, TOTAL and the question.
        """
        title, stamp = ink(WHITE, " UPDATE DAY"), ink(GREY, f"{WEEKDAYS[now().weekday()]} {now():%d.%m %H:%M} ")
        head = title + " " * max(1, self.columns - visible(title) - visible(stamp)) + stamp
        budget = max(1, self.lines - 1)
        small = 0 if self.log else 3  # the story may vanish; a table keeps its header, one row and "+ N more"

        def indent(page: list[str]) -> list[str]:
            return [line if line is head else f" {line}" if line else "" for line in page]

        for keys, gaps, notes in ((True, True, True), (False, True, True), (False, False, True), (False, False, False)):
            plan = {"keys": keys, "gaps": gaps, "notes": notes}
            base = len(self._page([head], room=small, **plan))
            if base > budget:
                continue
            page = self._page([head], room=None, **plan)
            return indent(page if len(page) <= budget else self._page([head], room=small + budget - base, **plan))
        total = self._total() if self.rows is not None and not self.log else []
        tail = [self.prompt] if self.prompt else []
        page = [head, *total[: max(0, budget - 1 - len(tail))], *tail]
        return indent(page[-budget:])

    def frame(self) -> str:
        """The whole page as text, every line cut to the screen."""
        return "\n".join(clip(line, self.columns) for line in self.lines_of_page()) + "\n"

    def draw(self, out: TextIO) -> None:
        with self.lock, contextlib.suppress(OSError, ValueError):
            if escapes(out):
                # Each line erases to its end, so a shorter line leaves nothing of the frame before it.
                out.write(FRAME_START + self.frame().replace("\n", "\x1b[K\n") + FRAME_END)
            else:
                out.write(self.frame())
            out.flush()


def summary(report: dict[str, Any], changes: list[Row] = ()) -> tuple[str, list[str]]:
    """How the run ended, in a headline and plain lines; every problem comes with its command."""
    changed = ", ".join(f"{STORE_NAMES.get(store, store)} {count}" for store, count in (report.get("updated") or {}).items())
    good = report.get("exit_code") in (0, 10)
    # "no new versions" says the machine was looked at and was current; after a failure all that is known is that nothing changed.
    lines = [ink(WHITE, f"updated: {changed}") if changed else ink(GREY, "no new versions" if good else "nothing was changed")]
    width = max([len(row.name) for row in changes] + [0]) + 3
    lines += [f"  {ink(WHITE, fit(row.name, width))}{ink(GREY, f'{row.old} → {row.new}')}" for row in changes]
    if report.get("crash"):
        lines.append(ink(RED, f"the updater itself broke: {report['crash']}"))
    for item in report.get("trouble") or []:
        lines.append(ink(RED, f"{human(item['probe'])}: {cause(item.get('detail') or '')}"))
        lines.append(f"  {ink(GREY, 'fix: ')}{ink(WHITE, item.get('fix') or '')}")
    held = report.get("held") or {}
    if "aur" in held:
        lines.append(f"{ink(GREY, 'AUR waits for you; by hand: ')}{ink(WHITE, auto.AUR_BY_HAND)}")
    if held.get("aur_too_fresh"):
        lines.append(ink(GREY, f"changed in AUR too recently, held this week: {', '.join(held['aur_too_fresh'])}"))
    for line in held.get("aur_by_hand") or []:  # "name: reason", built from the AUR's answer
        lines.append(f"{ink(GREY, 'AUR by hand, ')}{ink(WHITE, clean(line))}")
    lines += [f"{ink(GREY, 'new config version: ')}{ink(WHITE, path)}{ink(GREY, '  compare: sudo pacdiff')}" for path in report.get("pacnew") or []]
    headline = "DONE" if good else "FAILED"
    if report.get("reboot_required"):
        headline += " · REBOOT NEEDED"
    return ink(WHITE if good else RED, headline), lines


EOF_KEY = "\x04"  # what read_key returns when the input ended (Ctrl-D, or a closed stream): there is nobody to ask
ESC_WAIT = 0.05  # seconds a lone Esc waits to see whether it begins an arrow key or another sequence


def _escape_tail(fd: int) -> bytes:
    """What follows an Esc that has been read: the rest of an arrow key, F-key or Alt-key; b"" for a bare Esc."""
    tail = b""

    def more() -> bytes:
        return os.read(fd, 1) if select.select([fd], [], [], ESC_WAIT)[0] else b""

    first = more()
    tail += first
    if first == b"[":  # CSI: parameter bytes, then one final byte from @ to ~
        while byte := more():
            tail += byte
            if 0x40 <= byte[0] <= 0x7E:
                break
    elif first == b"O":  # SS3: F1-F4 and the application-mode arrows
        tail += more()
    return tail


def read_key(stream: TextIO, timeout: float | None = None, tick: Callable[[], None] | None = None) -> str | None:
    """One key without Enter on a terminal; one line otherwise. None if nothing came within `timeout` seconds.

    On a terminal the keys are taken one at a time for the whole wait, not only once something has arrived:
    in line mode nothing arrives before Enter, and switching modes afterwards would throw the key away.
    `tick` is called a few times a second while waiting, so the screen can follow a resized window.
    A key that sends several bytes (an arrow, Home, F5) is taken whole and is no answer: the wait goes on.
    A bare Esc is "not now". EOF_KEY means the input ended.
    """
    if not stream.isatty():
        if timeout is not None:
            try:
                ready = select.select([stream], [], [], max(0.0, timeout))[0]
            except (OSError, ValueError):  # not a real file (a test's buffer): it never blocks
                ready = [stream]
            if not ready:
                return None
        line = stream.readline()
        return line.strip()[:1].lower() if line else EOF_KEY
    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        while True:
            while not select.select([fd], [], [], 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic())))[0]:
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                if tick:
                    tick()
            byte = os.read(fd, 1)
            if not byte:
                return EOF_KEY
            if byte != b"\x1b":
                return byte.decode(errors="ignore").lower()
            if not _escape_tail(fd):
                return "\x1b"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def ask_wait(config: dict[str, Any] | None) -> float:
    """Seconds the question waits for an answer: `ask_timeout_minutes` in the config, 30 by default."""
    minutes = (config or {}).get("ask_timeout_minutes", ASK_WAIT_MINUTES)
    valid = isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0
    return float(minutes if valid else ASK_WAIT_MINUTES) * 60


def _record(outcome: str, code: int, reason: str = "") -> None:
    """Leave the answer where the timer's run can read it, whatever the terminal emulator reports."""
    with contextlib.suppress(OSError):
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now().astimezone().isoformat(), "outcome": outcome, "exit_code": code, "reason": reason}
        (STATE_ROOT / ASK_RESULT).write_text(json.dumps(entry) + "\n", encoding="utf-8")


def confirm(question: str, stdin: TextIO = sys.stdin, out: TextIO = sys.stdout) -> bool:
    """A plain yes/no on the line below the question; anything but y or yes is no."""
    out.write(f"{question} [y/N] ")
    out.flush()
    return stdin.readline().strip().lower() in ("y", "yes")


def last_log(rows: int) -> list[str]:
    """The tail of the last real run's log, cleaned of colour codes and progress redraws, in at most `rows` - 3 lines.

    A run that stopped at a check has a report but no log (nothing was run to write one): then its summary is shown.
    """
    try:
        report = load_json(STATE_ROOT / "last-auto.json")
        run_id = str(report.get("run_id", "?"))
    except (OSError, ValueError, AttributeError):
        return ["no run on record yet"]
    try:
        text = (STATE_ROOT / "runs" / run_id / "run.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        headline, lines = summary(report)
        return [clean(f"last run {run_id}: no commands ran, so there is no log"), "", headline, *lines][: max(3, rows - 3)]
    lines = [clean(line.split("\r")[-1]).rstrip() for line in text.splitlines()]
    return [clean(f"last run {run_id}"), "", *[line for line in lines if line][-max(1, rows - 5) :]]


def pick_kernel(stdin: TextIO, out: TextIO) -> None:
    """Key k: the kernel profiles, one pick, the same confirmation as `arch-update kernel`."""
    try:
        profiles = [profile for profile in available_kernel_profiles() if profile["id"] != "auto"]
        problem = "" if profiles else "no kernel profiles"
    except (OSError, ValueError, KeyError) as exc:
        profiles, problem = [], str(exc)
    lines = ["KERNEL", "", *(f"  {number}  {profile['label']:<24}{profile['package']}{'  (running)' if profile.get('detected') else ''}" for number, profile in enumerate(profiles, 1))]
    out.write(("\x1b[2J\x1b[H" if escapes(out) else "") + clean("\n".join([*lines, "", problem or "number to install, Enter to go back: "]) + ("\n[any key] back\n" if problem else "")))
    out.flush()
    if problem:
        read_key(stdin)
        return
    answer = stdin.readline().strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(profiles):
        return
    profile = profiles[int(answer) - 1]
    if not confirm(clean(f"Install kernel profile {profile['id']} and set it as the GRUB default?"), stdin, out):
        return
    try:
        result = select_kernel(profile["id"])
        said = [f"kernel set to {result.get('package')}"] if result.get("changed") else [f"no change: {result.get('reason', 'kernel unchanged')}"]
        said += ["reboot needed to boot into it; this tool does not reboot"] if result.get("reboot_required") else []
    except (RuntimeError, ValueError, OSError) as exc:
        said = [str(exc)]
    out.write(clean("\n".join(said)) + "\n[Enter] back\n")
    out.flush()
    stdin.readline()


def _root(sh: Shell, stdin: TextIO, out: TextIO) -> bool:
    """Can the run use sudo? If it needs a password and there is a terminal, ask for it here, once.

    The password goes to sudo on the terminal and nowhere else; it is not read, kept or logged here.
    """
    interactive = stdin.isatty() and out.isatty()
    if interactive:
        # Whether `sudo -n` passes on a rule or on a ticket this terminal session made earlier, the commands run in
        # this session: in a session of their own they have no ticket and `sudo -n pacman -Syu` would fail.
        sh.stay_in_session()
    if sh.capture(["sudo", "-n", "true"], timeout=10).returncode == 0:
        return True
    if not interactive:
        return False
    out.write("\x1b[?25h\n" if escapes(out) else "\n")
    out.flush()
    accepted = sh.interactive(["sudo", "-v"]) == 0
    out.write("\x1b[?25l" if escapes(out) else "")
    return accepted


def ask(
    sh: Shell | None = None,
    stdin: TextIO = sys.stdin,
    out: TextIO = sys.stdout,
    wait: float | None = None,
    always: bool = False,
    notice: str = "",
    look_only: bool | None = None,
    record: bool = False,
) -> int:
    """Show the screen; on yes run the update here and show how it ended. Returns the exit status.

    Without `always`, a queue that is read and empty ends the visit: nothing to ask. Without a terminal on
    `out` the frame is printed once and no key is read (`look_only`). Nobody answering is an answer too:
    after `wait` seconds (the config's `ask_timeout_minutes`) the week is skipped, so the window and its unit
    do not hang for ever.

    `record` is for the weekly window alone: its answer goes to ask-result.json, where the timer's run reads it
    (not now = 80, the status the unit accepts), and nothing is asked of a machine that cannot say what waits.
    A visit started by hand leaves no file and exits 0 on "not now"; only a question nobody answered is 80.
    """
    sh = sh or Shell()
    config = load_json(auto.CONFIG_PATH) if auto.CONFIG_PATH.is_file() else None
    wait = ask_wait(config) if wait is None else wait
    try:
        last = load_json(STATE_ROOT / "last-auto.json")
    except (OSError, ValueError):
        last = {}
    live = out.isatty()
    fancy = escapes(out)  # a dumb terminal is a terminal, but it can neither redraw nor clear
    global PLAIN
    PLAIN = not live
    look_only = not live if look_only is None else look_only
    screen = Screen(shutil.get_terminal_size((118, 34)))
    if look_only and not live:
        screen.lines = 10**6  # a pipe has no height: nothing is cut off
    screen.aur_on = (config or {}).get("unattended_aur") is True
    # With a person at the terminal the password is asked on yes, so a rule that was missing last time is not pending.
    typed = stdin.isatty() and live and sh.has("sudo") and os.geteuid() != 0
    screen.hanging = [f"{human(item['probe'])} → {item['fix']}" for item in last.get("trouble") or [] if not (typed and item.get("case") in NEEDS_PASSWORD)]
    screen.notes = [notice] if notice else []
    resized = threading.Event()
    main = live and threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGWINCH, lambda *_: resized.set()) if main else None
    stoppers = {number: signal.signal(number, _stop) for number in (signal.SIGTERM, signal.SIGHUP)} if main else {}
    if fancy:
        out.write("\x1b[?25l\x1b[2J")  # hide the cursor, start from a clean page
    shown = [""]

    def paint() -> None:
        if look_only:
            return
        if not fancy and live and (not screen.prompt or screen.prompt == shown[0]):
            return  # nothing can be redrawn on a dumb terminal: a frame is printed when its question or ending changes
        shown[0] = screen.prompt
        if resized.is_set():
            resized.clear()
            screen.resize(shutil.get_terminal_size((118, 34)))
            out.write("\x1b[2J" if fancy else "")
        screen.draw(out)

    def read_queue() -> None:
        screen.rows, screen.unread = queue(sh)
        screen.unread_reason = "install pacman-contrib" if not sh.has("checkupdates") else "checkupdates failed"
        paint()

    def answer(outcome: str, code: int, reason: str = "") -> None:
        if record:
            _record(outcome, code, reason)

    def leave(code: int) -> int:
        """End the visit that did not update. From the desktop entry the window would vanish with the screen: wait for Enter."""
        if os.environ.get(HOLD_ENV) and stdin.isatty() and live:
            screen.prompt = (screen.prompt + "   " if screen.prompt else "") + ink(DIM, "[Enter] close")
            screen.draw(out)
            with contextlib.suppress(KeyboardInterrupt, OSError, ValueError):
                stdin.readline()
        return code

    started = False  # the update itself began: from then on a stop leaves a half-done update, not nothing
    try:
        # The page is on screen at once; the queue and the look arrive when they are read.
        paint()
        reader = threading.Thread(target=read_queue, daemon=True)  # a Ctrl-C must not wait for checkupdates
        reader.start()
        screen.verdicts = auto.look(sh)  # on this thread: the probes install nothing, but a run installs a signal handler
        for verdict in screen.verdicts:  # no sudo rule yet is not a fault when the owner can type the password on yes
            if verdict["case"] == "NO-ROOT" and os.geteuid() != 0 and stdin.isatty() and live:
                verdict["status"] = "pass"
                screen.password_later, screen.no_sudo = sh.has("sudo"), not sh.has("sudo")
        screen.notes += [WEEKLY_OFF] if weekly_off(sh) else []
        paint()
        reader.join()
        if look_only or (not always and screen.rows == [] and not screen.unread):
            screen.prompt = ink(DIM, LOOK_ONLY) if look_only and (screen.rows or screen.unread) else ""
            screen.draw(out)
            return leave(0)
        waiting_for_a_click = bool(screen.rows) and not screen.aur_on and all(row.store == "aur" for row in screen.rows)
        if "repo" in screen.unread or (waiting_for_a_click and not screen.unread):
            # No question: with the repositories unread yes would run pacman -Syu blind, and with only the AUR waiting
            # (and the AUR off) it would do nothing anyone wanted.
            blind = "repo" in screen.unread
            screen.prompt = ink(GREY, unread_prompt(sh.has("checkupdates")) if blind else BY_HAND_ONLY)
            screen.draw(out)
            answer("unread" if blind else "nothing-to-do", exits.BLOCKED if blind else exits.IDLE, screen.unread_reason if blind else "only the AUR waits")
            return leave(exits.BLOCKED if blind and record else exits.IDLE)
        screen.prompt, screen.keys = f"{ink(WHITE, 'UPDATE NOW?')}  {ink(WHITE, '[y]')} {ink(GREY, 'yes')}   {ink(WHITE, '[N]')} {ink(GREY, 'not now')}   {ink(DIM, 'Enter means no')}", True
        deadline = time.monotonic() + wait
        while True:
            paint()
            if (key := read_key(stdin, deadline - time.monotonic(), tick=lambda: paint() if resized.is_set() else None)) is None:
                reason = f"no answer within {wait / 60:g} minutes"
                screen.prompt, screen.keys = ink(GREY, f"{reason}: skipped this week; by hand: arch-update"), False
                screen.draw(out)
                answer("timed-out", exits.SKIPPED, reason)
                return exits.SKIPPED
            if key == EOF_KEY:  # the input ended: nobody answered, and an Enter nobody pressed is not "not now"
                reason = "no input: standard input ended before an answer"
                screen.prompt, screen.keys = ink(RED, f"{reason}; nothing was changed; by hand: arch-update"), False
                screen.draw(out)
                answer("no-input", exits.TOOL_FAILURE, reason)
                return exits.TOOL_FAILURE
            if key not in ("l", "k"):
                break
            out.write("\x1b[?25h" if fancy else "")
            if key == "l":
                out.write(("\x1b[2J\x1b[H" if fancy else "") + "\n".join(f" {clip(line, screen.columns - 1)}" for line in last_log(screen.lines)) + "\n\n [any key] back\n")
                out.flush()
                read_key(stdin)
            else:
                pick_kernel(stdin, out)
            out.write("\x1b[?25l\x1b[2J" if fancy else "")
        screen.keys = False
        if key != "y":
            screen.prompt = ink(GREY, "not now. I will ask again next time; by hand: arch-update")
            screen.draw(out)
            answer("declined", exits.SKIPPED, "answered not now")
            return leave(exits.SKIPPED if record else 0)
        if not _root(sh, stdin, out):
            asked = stdin.isatty() and live
            reason = "sudo did not accept the password" if asked else "sudo needs a password and there is no terminal to ask on"
            screen.prompt = ink(RED, f"{reason}; nothing was changed" + ("" if asked else ". Fix: arch-update auto --sudoers"))
            screen.draw(out)
            answer("no-sudo", exits.TOOL_FAILURE, reason)
            return leave(exits.TOOL_FAILURE)

        def event(kind: str, details: dict[str, Any]) -> None:
            screen.event(kind, details)
            paint()

        screen.prompt = ink(GREY, "running …")
        started = True
        with Keepalive(sh, enabled=stdin.isatty() and live) as keep:
            report = auto.run_auto(sh=sh, watcher=event)
        if exits.interrupted(report.get("exit_code")):
            raise exits.Stopped(report.get("signal") or signal.SIGINT, report.get("child_stopped"))
        if report.get("exit_code") == exits.SKIPPED:  # the weekly unit, or a second Update Day, holds the lock: nothing ran
            screen.log.append(ink(WHITE, "another update is running; nothing was changed"))
            screen.prompt = ""
            paint()
            answer("busy", exits.SKIPPED, "another run holds the lock")
            return leave(exits.SKIPPED)
        screen.prompt = ink(GREY, "reading what changed …")
        paint()
        after, unread = queue(sh) if report["updated"] else ([], ["all"])
        still = {(row.name, row.new) for row in after}
        changes = [row for row in screen.rows or [] if report["updated"].get(row.store) and row.store not in unread and (row.name, row.new) not in still]
        headline, lines = summary(report, changes)
        if keep.expired:
            lines.append(ink(RED, TICKET_EXPIRED))
        screen.log += ["", *lines]
        screen.prompt = f"{headline}   {ink(DIM, '[Enter] close')}"
        paint()
        answer("updated", report["exit_code"])
        with contextlib.suppress(KeyboardInterrupt):  # the update is over; a Ctrl-C at 'close' only closes
            stdin.readline()
        return report["exit_code"]
    except KeyboardInterrupt as stop:  # Ctrl-C, SIGTERM, SIGHUP: the page is finished off, never left half drawn
        code = 128 + getattr(stop, "signum", signal.SIGINT)
        said = interruption(started, getattr(stop, "child_stopped", None))
        screen.prompt = ink(RED, said)
        screen.draw(out)
        answer("interrupted", code, said)
        return leave(code)
    finally:
        for number, handler in stoppers.items():
            signal.signal(number, handler)
        if previous is not None:
            signal.signal(signal.SIGWINCH, previous)
        PLAIN = False
        if fancy:
            out.write("\x1b[?25h")


def _say(sh: Shell, message: str) -> None:
    """A run nobody hears about is lost: put it in the window log and, if there is a desktop, on the screen."""
    with contextlib.suppress(OSError):
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        with (STATE_ROOT / ASK_LOG).open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().astimezone().isoformat()} {message}\n")
    print(f"arch-update: {message}", file=sys.stderr)
    if sh.has("notify-send"):
        sh.capture(["notify-send", "--urgency=critical", "arch-update: weekly update did not run", message[:400]], timeout=10)


def scheduled(sh: Shell | None = None) -> int:
    """What the weekly timer runs: the question in a window if the owner wants to be asked, else the update.

    Exit status, by what happened:
      the update ran                         its own status (IDLE 0, READY_FOR_REBOOT 10, BLOCKED 20, ...)
      no terminal installed                  the update runs unasked, so its own status
      terminal found, no screen to open it   BLOCKED (20), notification, line in ask-window.log
      answered "not now" or no answer in
      `ask_timeout_minutes`                  SKIPPED (80), reason in ask-result.json
      window gone with no answer on record   TOOL_FAILURE (70), notification, line in ask-window.log
    The terminal emulator's own exit status is never used: emulators differ on whether they pass it on.
    """
    sh = sh or Shell()
    config = load_json(auto.CONFIG_PATH) if auto.CONFIG_PATH.is_file() else None
    reason = quiet.gate(config)
    waiting = quiet.waited(reason, STATE_ROOT)
    if waiting:  # not a visit: the timer asks again tomorrow, for a few days at most
        print(f"arch-update: waiting, {waiting}")
        return exits.SKIPPED
    if reason:
        print(f"arch-update: waited {quiet.MAX_WAIT_DAYS} days ({reason}); updating anyway, under the ceiling")
    terminal = sh.terminal() if (config or {}).get("ask") is True else None
    if not terminal:
        for line in quiet.calm(config):  # nobody is watching this one: a ceiling, so it is not heard either
            print(f"arch-update: quiet: {line}")
        try:
            return auto.run_auto(sh=sh)["exit_code"]
        finally:
            quiet.release()  # systemd would keep it until the next boot
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        _say(sh, f"{terminal[0]} cannot open: no DISPLAY or WAYLAND_DISPLAY in this session; the weekly update was not run. By hand: arch-update auto")
        return exits.BLOCKED
    window = [terminal[0], *WINDOW.get(terminal[0], []), *terminal[1:], sh.path("arch-update"), "auto", "--ask", "--record"]
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    (STATE_ROOT / ASK_RESULT).unlink(missing_ok=True)
    # No time limit on the window: closing it under a running update would cut the update.
    # What bounds it is the question's own wait (`ask`).
    sh.logged(window, STATE_ROOT / ASK_LOG, None)
    try:
        answer = json.loads((STATE_ROOT / ASK_RESULT).read_text(encoding="utf-8"))
        return int(answer["exit_code"])
    except (OSError, ValueError, KeyError, TypeError):
        _say(sh, "the question window closed without an answer; the weekly update was not run. By hand: arch-update auto")
        return exits.TOOL_FAILURE
