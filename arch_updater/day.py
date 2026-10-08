"""Update day: the screen that asks before the weekly update, then shows it run.

A dnf-style transaction in plain words: what will change and where it comes
from, whether the machine is in order, what still hangs from last time and the
command that fixes it, one question. Nothing on it needs explaining to someone
who has never heard of the engine. Key t adds the engine's own view: the two
nodes of control.tis with the instruction each is on.

Plain ANSI in black, grey and white, with red for what is wrong. One write per
frame inside a synchronized update, so a GPU terminal paints it whole. The
update queue is read here only to be shown.
"""

from __future__ import annotations

import contextlib
import os
import json
import re
import select
import shutil
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TextIO

from . import auto, exits
from .engine import STATE_ROOT, load_json
from .shell import Shell

WHITE, GREY, DIM, RED = (230, 230, 230), (128, 128, 128), (64, 64, 64), (208, 49, 45)
FRAME_START, FRAME_END = "\x1b[?2026h\x1b[H", "\x1b[J\x1b[?2026l"  # synchronized update: the frame lands whole
NODE_WIDTH = 34
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
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Where each store's queue can be read, and the exit codes that mean "read fine".
QUEUES = (
    ("repo", "checkupdates", ["checkupdates"], (0, 2)),
    ("aur", "yay", ["yay", "-Qua"], (0, 1)),
    ("aur", "paru", ["paru", "-Qua"], (0, 1)),
)
STORE_NAMES = {"repo": "repositories", "aur": "AUR", "flatpak": "flatpak", "snap": "snap"}
ASK_WAIT_MINUTES = 30  # how long the question waits for an answer before the week is skipped
ASK_RESULT = "ask-result.json"
ASK_LOG = "ask-window.log"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
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


def human(probe: str) -> str:
    return CHECKS.get(probe, probe)


@dataclass(frozen=True)
class Row:
    name: str
    old: str
    new: str
    store: str


def queue(sh: Shell) -> tuple[list[Row], list[str]]:
    """What is waiting, for display only, and the stores whose queue could not be read.

    The repository queue needs checkupdates (pacman-contrib); without it the queue is unread, not empty.
    """
    rows: list[Row] = []
    unread: list[str] = []
    seen: set[str] = set()
    for store, tool, argv, quiet in QUEUES:
        if store == "repo" and not sh.has(tool):
            unread.append(store)
            continue
        if store in seen or not sh.has(tool):
            continue
        seen.add(store)
        result = sh.capture(argv, timeout=180)
        if result.returncode not in quiet:
            unread.append(store)
            continue
        rows += [Row(m[1], m[2], m[3], store) for m in map(_QUEUE_LINE.match, result.output.splitlines()) if m]
    return rows, unread


def colour() -> bool:
    return not os.environ.get("NO_COLOR")


def ink(rgb: tuple[int, int, int], text: str, *, on: tuple[int, int, int] | None = None) -> str:
    if not colour() or not text:
        return text
    back = f";48;2;{on[0]};{on[1]};{on[2]}" if on else ""
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}{back}m{text}\x1b[0m"


def visible(text: str) -> int:
    return len(_ANSI.sub("", text))


def fit(text: str, width: int) -> str:
    """Pad or cut plain text to exactly `width` columns."""
    return text[: width - 1] + "…" if len(text) > width else text.ljust(width)


def listing(program: str) -> dict[str, list[tuple[str, str]]]:
    """Each node's instructions as written, with the comment that explains each."""
    nodes: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for raw in program.splitlines():
        code, _, comment = raw.partition("#")
        if code.strip().startswith("@"):
            current = nodes.setdefault(code.split()[0][1:], [])
        elif current is not None and code.strip():
            current.append((code.rstrip(), comment.strip()))
    return nodes


class Screen:
    """Everything on the page. Events change it; frame() draws what it holds."""

    def __init__(self, program: str, size: tuple[int, int]) -> None:
        self.code = listing(program)
        self.columns, self.lines = max(72, min(size[0], 124)), size[1]
        self.rows: list[Row] | None = None  # None: still reading
        self.unread: list[str] = []
        self.unread_reason = "install pacman-contrib"
        self.verdicts: list[dict[str, Any]] | None = None
        self.aur_on = False
        self.hanging: list[str] = []
        self.pc: dict[str, int] = {}
        self.acc: dict[str, int] = {}
        self.ports: dict[str, int] = {}
        self.log: list[str] = []  # what the run has done so far; replaces the table once it starts
        self.prompt = ""
        self.engine = False  # the two nodes of the program, for whoever wants to watch it execute: key t
        self.lock = threading.Lock()

    # ── events ──────────────────────────────────────────────────────────────

    def step(self, node: str, index: int, acc: int) -> None:
        self.pc[node], self.acc[node] = index, acc

    def event(self, kind: str, details: dict[str, Any]) -> None:
        if kind == "token":
            port = details["source"] if details["source"] not in self.code else details["target"]
            if port not in self.code:
                self.ports[port] = details["value"]
            return
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

    def _left(self, width: int, height: int) -> list[str]:
        if self.log:
            return [f" {line}" for line in self.log[-height:]]
        name_w = max(16, width - 52)
        head = ink(GREY, f" {fit('PACKAGE', name_w)}{fit('INSTALLED', 18)}{fit('AVAILABLE', 18)}FROM")
        if self.rows is None:
            return [head, ink(DIM, " " + "─" * (width - 2)), ink(GREY, " reading the queue …")]
        body = self.rows if len(self.rows) <= height - 4 else self.rows[: height - 5]
        lines = [head, ink(DIM, " " + "─" * (width - 2))]
        for row in body:
            source = row.store.upper() + ("" if row.store != "aur" or self.aur_on else " · BY HAND")
            lines.append(f" {ink(WHITE, fit(row.name, name_w))}{ink(GREY, fit(row.old, 18))}{ink(WHITE, fit(row.new, 18))}{ink(GREY, fit(source, 13).rstrip())}")
        if len(self.rows) > len(body):
            lines.append(ink(GREY, f" … and {len(self.rows) - len(body)} more    [l] full list"))
        if "repo" in self.unread:
            lines.append(ink(RED, f" cannot check the repositories: {self.unread_reason}"))
        elif not self.rows:
            lines.append(ink(GREY, " nothing is waiting; the run still checks the machine"))
        counts = {store: sum(row.store == store for row in self.rows) for store in ("repo", "aur")}
        total = f"{'?' if 'repo' in self.unread else counts['repo']} from repositories · {counts['aur']} from AUR" + ("" if self.aur_on or not counts["aur"] else " (wait for your click)")
        total += "".join(f" · the {store} queue could not be read" for store in self.unread)
        return [*lines, ink(DIM, " " + "─" * (width - 2)), f" {ink(GREY, 'TOTAL    ')}{ink(WHITE, total)}"]

    def _node(self, name: str) -> list[str]:
        inner = NODE_WIDTH - 2
        acc = f" ACC {self.acc.get(name, 0):>4} "
        lines = [ink(DIM, "┌─") + ink(WHITE, f" @{name} ") + ink(DIM, "─" * (inner - len(name) - 4 - len(acc) - 1)) + ink(GREY, acc) + ink(DIM, "┐")]
        for index, (code, _) in enumerate(self.code[name]):
            active = self.pc.get(name) == index
            text = fit(code.expandtabs(8), inner - 1)
            cell = ink((0, 0, 0), "▶" + text, on=WHITE) if active and colour() else ("▶" if active else " ") + ink(GREY, text)
            lines.append(ink(DIM, "│") + cell + ink(DIM, "│"))
        return [*lines, ink(DIM, "└" + "─" * inner + "┘")]

    def _right(self) -> list[str]:
        return [line for name in self.code for line in self._node(name)]

    def _status(self, width: int) -> list[str]:
        bad = [v for v in self.verdicts or [] if v["status"] != "pass"]
        if self.verdicts is None:
            machine = ink(GREY, "checking …")
        elif bad:
            machine = ink(RED, fit("; ".join(f"{human(v['probe'])}: {v['detail']}" for v in bad), width - 10).rstrip())
        else:
            machine = ink(WHITE, "fine") + ink(GREY, f" · {len(self.verdicts)} checks")
        hanging = ink(RED, fit("; ".join(self.hanging), width - 10).rstrip()) if self.hanging else ink(GREY, "nothing since last time")
        active = self.pc.get("CTL")
        thought = self.code["CTL"][active][1] if active is not None and "CTL" in self.code else ""
        ports = "  ".join(f"{ink(GREY, port)} {ink(RED if value < 0 else WHITE, str(value))}" for port, value in self.ports.items())
        return [
            f" {ink(GREY, 'MACHINE  ')}{machine}",
            f" {ink(GREY, 'PENDING  ')}{hanging}",
            f" {ink(GREY, 'PORTS    ')}{ports}  {ink(DIM, '▸ ' + thought)}" if self.engine and self.log else "",
            f" {self.prompt}",
        ]

    def frame(self) -> str:
        """The whole page as text, every line cut to the screen."""
        two = self.engine and self.columns >= 100
        left_w = self.columns - (NODE_WIDTH + 3 if two else 0)
        now = datetime.now().astimezone()
        title, stamp = ink(WHITE, " UPDATE DAY"), ink(GREY, f"{WEEKDAYS[now.weekday()]} {now:%d.%m %H:%M} ")
        top = [title + " " * max(1, self.columns - visible(title) - visible(stamp)) + stamp, ink(DIM, "━" * self.columns)]
        status = self._status(self.columns)
        height = max(6, self.lines - len(top) - len(status) - 2)
        left, right = self._left(left_w, height), (self._right() if two else [])
        body = []
        for index in range(max(len(left), len(right)) if two else len(left)):
            cell = left[index] if index < len(left) else ""
            pad = " " * max(0, left_w - visible(cell))
            body.append(cell + pad + ("  " + right[index] if two and index < len(right) else ""))
        return "\n".join([*top, *body[:height], ink(DIM, "━" * self.columns), *status]) + "\n"

    def draw(self, out: TextIO) -> None:
        with self.lock, contextlib.suppress(OSError, ValueError):
            if out.isatty():
                # Each line erases to its end, so a shorter line leaves nothing of the frame before it.
                out.write(FRAME_START + self.frame().replace("\n", "\x1b[K\n") + FRAME_END)
            else:
                out.write(self.frame())
            out.flush()


def summary(report: dict[str, Any]) -> tuple[str, list[str]]:
    """How the run ended, in a headline and plain lines; every problem comes with its command."""
    changed = ", ".join(f"{STORE_NAMES.get(store, store)} {count}" for store, count in report["updated"].items())
    lines = [ink(WHITE, f"updated: {changed}") if changed else ink(GREY, "no new versions")]
    if report.get("crash"):
        lines.append(ink(RED, f"the updater itself broke: {report['crash']}"))
    for item in report["trouble"]:
        lines.append(ink(RED, f"{human(item['probe'])}: {(item['detail'].splitlines() or [''])[-1]}"))
        lines.append(f"  {ink(GREY, 'fix: ')}{ink(WHITE, item['fix'])}")
    if "aur" in report["held"]:
        lines.append(f"{ink(GREY, 'AUR waits for you: click the notification or run ')}{ink(WHITE, auto.AUR_BY_HAND)}")
    if report["held"].get("aur_too_fresh"):
        lines.append(ink(GREY, f"changed in AUR too recently, held this week: {', '.join(report['held']['aur_too_fresh'])}"))
    lines += [f"{ink(GREY, 'new config version: ')}{ink(WHITE, path)}{ink(GREY, '  compare: sudo pacdiff')}" for path in report["pacnew"]]
    good = report["exit_code"] in (0, 10)
    headline = "DONE" if good else "FAILED"
    if report["reboot_required"]:
        headline += " · REBOOT NEEDED"
    return ink(WHITE if good else RED, headline), lines


def read_key(stream: TextIO, timeout: float | None = None) -> str | None:
    """One key without Enter on a terminal; one line otherwise. None if nothing came within `timeout` seconds."""
    if timeout is not None:
        try:
            ready = select.select([stream], [], [], max(0.0, timeout))[0]
        except (OSError, ValueError):  # not a real file (a test's buffer): it never blocks
            ready = [stream]
        if not ready:
            return None
    if not stream.isatty():
        return stream.readline().strip()[:1].lower()
    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return stream.read(1).lower()
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


def ask(sh: Shell | None = None, stdin: TextIO = sys.stdin, out: TextIO = sys.stdout, wait: float | None = None) -> int:
    """Show the question; on yes run the update here and show how it ended. Returns the exit status.

    Nobody answering is an answer too: after `wait` seconds (the config's `ask_timeout_minutes`)
    the week is skipped, with the reason recorded, so the window and its unit do not hang for ever.
    """
    sh = sh or Shell()
    config = load_json(auto.CONFIG_PATH) if auto.CONFIG_PATH.is_file() else None
    wait = ask_wait(config) if wait is None else wait
    try:
        last = load_json(STATE_ROOT / "last-auto.json")
    except (OSError, ValueError):
        last = {}
    size = shutil.get_terminal_size((118, 34))
    screen = Screen(auto.PROGRAM.read_text(encoding="utf-8"), (size.columns, size.lines))
    screen.aur_on = (config or {}).get("unattended_aur") is True
    screen.hanging = [f"{item['probe']} → {item['fix']}" for item in last.get("trouble") or []]
    if out.isatty():
        out.write("\x1b[?25l\x1b[2J")  # hide the cursor, start from a clean page

    def read_queue() -> None:
        screen.rows, screen.unread = queue(sh)
        screen.unread_reason = "install pacman-contrib" if not sh.has("checkupdates") else "checkupdates failed"
        screen.draw(out)

    # The page is on screen at once; the queue and the look arrive when they are read.
    screen.prompt = ink(GREY, "…")
    screen.draw(out)
    reader = threading.Thread(target=read_queue)
    reader.start()
    screen.verdicts = auto.run_auto(dry_run=True, sh=sh)["verdicts"]  # on this thread: a run installs a signal handler
    screen.draw(out)
    reader.join()
    try:
        screen.prompt = f"{ink(WHITE, 'UPDATE NOW?')}  {ink(WHITE, '[y]')} {ink(GREY, 'yes')}   {ink(WHITE, '[N]')} {ink(GREY, 'not now')}   {ink(DIM, 'Enter means no')}"
        deadline = time.monotonic() + wait
        while True:
            screen.draw(out)
            key = read_key(stdin, deadline - time.monotonic())
            if key is None:
                reason = f"no answer within {wait / 60:g} minutes"
                screen.prompt = ink(GREY, f"{reason}: skipped this week; by hand: arch-update auto")
                screen.draw(out)
                _record("timed-out", exits.SKIPPED, reason)
                return exits.SKIPPED
            if key == "t":
                screen.engine = not screen.engine
                continue
            if key != "l" or not screen.rows:
                break
            out.write("\x1b[2J\x1b[H" + "".join(f" {row.name}  {row.old} → {row.new}  {row.store}\n" for row in screen.rows) + "\n [any key] back\n")
            out.flush()
            read_key(stdin)
        if key != "y":
            screen.prompt = ink(GREY, "not now. I will ask again next time; by hand: arch-update auto")
            screen.draw(out)
            _record("declined", exits.SKIPPED, "answered not now")
            return 0

        def step(node: str, index: int, acc: int) -> None:
            screen.step(node, index, acc)
            screen.draw(out)

        def event(kind: str, details: dict[str, Any]) -> None:
            screen.event(kind, details)
            screen.draw(out)

        screen.prompt = ink(GREY, "running …")
        report = auto.run_auto(sh=sh, watcher=event, step=step)
        headline, lines = summary(report)
        screen.log += ["", *lines]
        screen.prompt = f"{headline}   {ink(DIM, '[Enter] close')}"
        screen.draw(out)
        _record("updated", report["exit_code"])
        stdin.readline()
        return report["exit_code"]
    finally:
        if out.isatty():
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
    terminal = sh.terminal() if (config or {}).get("ask") is True else None
    if not terminal:
        return auto.run_auto(sh=sh)["exit_code"]
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        _say(sh, f"{terminal[0]} cannot open: no DISPLAY or WAYLAND_DISPLAY in this session; the weekly update was not run. By hand: arch-update auto")
        return exits.BLOCKED
    window = [terminal[0], *WINDOW.get(terminal[0], []), *terminal[1:], sh.path("arch-update"), "auto", "--ask"]
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
