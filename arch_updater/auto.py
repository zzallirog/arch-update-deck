"""The unattended weekly update. control.tis is the engine; this file is its ports.

WATCH looks at the machine, APPLY runs every installed store's own update,
CENSUS asks whether a failure has happened before and runs the repair that is
on record for it. Nothing here decides what happens next: that is the
program's job.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import census, exits, repairs, stores, tis, watch
from .cve import scan_cves
from .engine import STATE_ROOT, CommandResult, _write_json, aur_cache_root, load_json
from .exits import Stopped
from .shell import Shell
from .textsafe import cause, clean
from .watch import Verdict

PROGRAM = Path(__file__).with_name("control.tis")
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "arch-updater" / "auto.json"
STARTER_CONFIG = {"conditions": [], "unattended_aur": False}
KEEP_RUNS = 4
SUDOERS_FILE = "/etc/sudoers.d/zzz-arch-update-auto"
AUR_BY_HAND = "yay -Sua"
# Runs the notifier given as its arguments and waits; a click on the notice answers "default".
# Only then does it open $OPEN (a terminal) on $FIX, and keeps the window until Enter.
CLICK_SCRIPT = 'choice=$("$@"); [ "$choice" = default ] && exec $OPEN sh -c "$FIX; printf \'\\n[enter] \'; read _"'
RUN_ID = re.compile(r"auto-\d{8}T\d{6}[+-]\d{4}(-\d+)?")


class World:
    """What the two nodes are wired to: three ports and what each remembers."""

    def __init__(
        self, sh: Shell, config: dict[str, Any] | None, cases: list[census.Case], run_id: str, run_dir: Path | None, dry_run: bool = False
    ) -> None:
        self.sh = sh
        self.config = config
        self.cases = cases
        self.run_id = run_id
        self.run_dir = run_dir  # None: a look kept in memory, nothing of it is written
        self.dry_run = dry_run
        self.baseline: dict[str, Any] | None = None  # the machine at the first look
        self.verdicts: list[Verdict] = []
        self.blocker: Verdict | None = None  # what the look that decided this run stopped on
        self.failed: dict[str, Verdict] = {}  # stores whose update failed and has not succeeded since
        self.done: set[str] = set()  # stores whose update succeeded in this run
        self.updated: dict[str, int] = {}
        self.held: dict[str, Any] = {}  # what this run deliberately left alone, and why
        self.repairs: list[dict[str, Any]] = []
        self.failure = 0  # the token last written to CENSUS
        self.output = ""  # what the last failed command printed
        self.rebuild: list[str] = []
        self.pacnew: list[str] = []
        self.reboot = False
        self.kept = False
        self.dirty = False  # something changed since the last look
        self.stop = False  # SIGTERM arrived: finish the running command, start nothing new
        self.signum = 0  # the signal that asked for the stop, 0 if none did
        self.interrupted = False  # Ctrl-C, SIGTERM or SIGHUP ended the run: the running command was let finish, nothing else ran
        self.child_stopped: bool | None = None  # of the command that was running then: did a signal end it (None: none was running)
        self.looks = 0
        self.watcher: Callable[[str, dict[str, Any]], None] = lambda kind, details: None  # someone following the run live

    # ── evidence ────────────────────────────────────────────────────────────

    def note(self, kind: str, **details: Any) -> None:
        record = {"at": datetime.now().astimezone().isoformat(), "kind": kind, **details}
        if self.run_dir is not None:
            with (self.run_dir / "tokens.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        with contextlib.suppress(Exception):  # a screen that fails to draw must not stop an update half way
            self.watcher(kind, details)

    def trace(self, source: str, target: str, value: int) -> None:
        self.note("token", source=source, target=target, value=value)

    def command(self, argv: list[str], timeout: int | None = None) -> CommandResult:
        log = self.run_dir / "run.log"
        result = self.sh.logged(argv, log) if timeout is None else self.sh.logged(argv, log, timeout)
        if self.sh.interrupted:  # Ctrl-C (or the screen's SIGHUP) while it ran: it has ended now, and nothing else is started
            self.sh.interrupted = False
            raise Stopped(self.sh.signum, exits.ended_by_signal(result.returncode, self.sh.sent))
        if self.signum:  # SIGTERM while it ran: it was left alone and has ended on its own
            raise Stopped(self.signum, exits.ended_by_signal(result.returncode))
        return result

    # ── ports ───────────────────────────────────────────────────────────────

    def read(self, port: str) -> int:
        return {"WATCH": self._watch, "APPLY": self._apply, "CENSUS": self._census}[port]()

    def write(self, port: str, value: int) -> None:
        if port != "CENSUS":
            raise KeyError(f"nothing is wired to take a write on {port}")
        self.failure = value

    def look(self) -> list[Verdict]:
        """Ask every probe and take in what they learned. Returns the verdicts; decides nothing."""
        self.looks += 1
        self.dirty = False
        scene = watch.Scene(self.sh, self.config, CONFIG_PATH, self.keep_root(), self.baseline)
        reading = watch.look(scene)
        self.baseline = self.baseline or reading.facts.get("state")
        self.pacnew = reading.facts.get("pacnew", self.pacnew)
        self.reboot = reading.facts.get("reboot", self.reboot)
        self.rebuild = reading.facts.get("rebuild", [])
        self.verdicts = reading.verdicts
        self.note("look", verdicts=[asdict(verdict) for verdict in self.verdicts])
        return self.verdicts

    def _watch(self) -> int:
        """One number for the whole look: the first verdict that is not a pass decides."""
        self.blocker = next((verdict for verdict in self.look() if verdict.status != "pass"), None)
        if self.blocker is None:
            return 1
        if self.blocker.status != "fail":
            return 0
        return census.token(self.cases, self._case(self.blocker.case))

    def _case(self, case_id: str) -> census.Case:
        try:
            return census.find(self.cases, case_id)
        except StopIteration:
            raise KeyError(f"{case_id} is not in the census") from None

    def _apply(self) -> int:
        """Run each installed store's update once. One that fails does not stop the others."""
        moved = False
        for store in stores.STORES:
            if self.dry_run or store.name in self.done or not self.sh.has(store.tool):
                continue
            if store.name in self.failed and not self._case(self.failed[store.name].case).repair:
                continue  # it failed for a reason nothing here can repair: once is enough
            argv = self._argv(store)
            if argv is None:
                continue
            if not self.kept and not self._keep():
                break  # no record, no update
            before = self.sh.capture(list(store.listing), timeout=120)
            if self.stop:  # only the signal handler sets it, and it sets the number with it
                raise Stopped(self.signum)
            self.note("store", name=store.name, phase="start")
            result = self.command(repairs.sudo(argv) if store.root else argv)
            after = self.sh.capture(list(store.listing), timeout=120)
            changed = _packages_changed(before.output, after.output) if before.returncode == after.returncode == 0 else 0
            self.note("store", name=store.name, phase="done", changed=changed, ok=result.returncode == 0)
            if changed:
                self.updated[store.name] = self.updated.get(store.name, 0) + changed
                self.dirty = moved = True
            if result.returncode != 0:
                self.output = result.output
                self._fail(store.name, self._recognise(result.output, store.name), result.output)
                continue
            self.done.add(store.name)
            self.failed.pop(store.name, None)
        if self.failed:
            # Hand the census the failure it can do something about, if there is one.
            verdict = next((item for item in self.failed.values() if self._case(item.case).repair), next(iter(self.failed.values())))
            return census.token(self.cases, self._case(verdict.case))
        return 1 if moved else 0

    def _argv(self, store: stores.Store) -> list[str] | None:
        """The store's update command for this run, or None when the store is left alone."""
        argv = list(store.update)
        if store.name != "aur":
            return argv
        if (self.config or {}).get("unattended_aur") is not True:
            self.held["aur"] = f"unattended AUR is off; run `{AUR_BY_HAND}`"
            return None
        try:
            fresh = stores.aur_too_fresh(self.sh, float((self.config or {}).get("aur_min_age_days", stores.AUR_MIN_AGE_DAYS)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._fail("aur", self._case("AUR-UNREACHABLE"), f"{type(exc).__name__}: {exc}")
            return None
        if fresh:
            self.held["aur_too_fresh"] = fresh
            argv += ["--ignore", ",".join(fresh)]
        return argv

    def _recognise(self, output: str, where: str) -> census.Case:
        """The census case this failure belongs to; a failure nobody has seen becomes a new one."""
        case = census.recognise(self.cases, output)
        if case is None:
            return census.record(self.cases, STATE_ROOT, output, where, self.run_dir / "run.log")
        census.met_again(self.cases, STATE_ROOT, case)
        return case

    def _fail(self, store: str, case: census.Case, detail: str) -> None:
        self.failed[store] = Verdict(store, "fail", case.id, detail.strip()[-300:])

    def _census(self) -> int:
        """Has this failure happened before, and is there a repair on record for it?"""
        case = census.at(self.cases, self.failure)
        action = repairs.ACTIONS.get(case.repair) if case and case.repair else None
        attempted = bool(action) and not self.dry_run and not self.stop and action(self)
        self.repairs.append({"case": case.id if case else None, "repair": case.repair if case else None, "attempted": attempted})
        self.note("census", case=case.id if case else None, attempted=attempted)
        return 1 if attempted else 0

    # ── the record made before the first change ─────────────────────────────

    def keep_root(self) -> Path:
        """Where the pre-update record lands: the configured disk, or beside the run."""
        keep = (self.config or {}).get("keep")
        return Path(keep) if keep else (self.run_dir.parent.parent if self.run_dir else STATE_ROOT) / "keep"

    def _keep(self) -> bool:
        """Once, before the first change: what was installed, and the readable part of /etc.

        Enough to rebuild the package set on a fresh install. Files only root
        can read stay out, so the record holds no password hashes. Older
        records are pruned only after this one is whole, and never this one.
        """
        root = self.keep_root()
        target = root / self.run_id
        try:
            # The look has already refused a folder whose nearest existing parent is on the system disk
            # when another disk was asked for, so a vanished disk is not recreated on this one.
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.mkdir(mode=0o700, exist_ok=True)
            for name, flags, quiet in (("packages.txt", "-Q", (0,)), ("explicit.txt", "-Qqe", (0,)), ("foreign.txt", "-Qqm", (0, 1))):
                listing = self.sh.capture(["pacman", flags], timeout=60)
                if listing.returncode not in quiet:  # -Qqm exits 1 when there are no foreign packages
                    raise OSError(f"pacman {flags} failed: {listing.output.strip()[-200:]}")
                (target / name).write_text(listing.output, encoding="utf-8")
            tar = ["tar", "--zstd", "--ignore-failed-read", "--warning=no-failed-read", "-cf", str(target / "etc.tar.zst"), "-C", "/", "etc"]
            if self.command(tar, timeout=600).returncode not in (0, 1):  # 1: a file changed while being read
                raise OSError(f"could not archive /etc into {target}")
        except OSError as exc:
            self.blocker = Verdict("keep", "blocked", "KEEP-UNWRITABLE", str(exc), exits.BACKUP_BLOCKED)
            self.failed["keep"] = self.blocker
            return False
        older = sorted(path for path in root.iterdir() if RUN_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink() and path != target)
        for stale in older[: max(0, len(older) - (KEEP_RUNS - 1))]:
            shutil.rmtree(stale, ignore_errors=True)
        self.kept = True
        self.note("keep", path=str(target))
        return True


def exit_code(out: int, world: World) -> int:
    if out > 0:
        return exits.READY_FOR_REBOOT if world.reboot else exits.IDLE
    if "keep" in world.failed:
        return exits.BACKUP_BLOCKED
    if world.failed:
        return exits.RECOVERY_PENDING
    if world.blocker is None:
        return exits.BLOCKED
    return exits.RECOVERY_PENDING if world.blocker.status == "fail" else world.blocker.code


def cve_axis() -> dict[str, Any]:
    """Vulnerabilities are reported beside the verdict, never folded into it."""
    scan = scan_cves()
    if not scan.get("available") or scan.get("error"):
        return {"known": False, "error": scan.get("error")}
    severities: dict[str, int] = {}
    for finding in scan["findings"]:
        severity = str(finding.get("severity", "Unknown")) if isinstance(finding, dict) else "Unknown"
        severities[severity] = severities.get(severity, 0) + 1
    return {"known": True, "findings": len(scan["findings"]), "by_severity": severities}


def sudoers_rules(user: str, aur: bool = False, sh: Shell | None = None) -> str:
    """The sudoers lines an unattended run needs, built from the commands it runs.

    Without `aur` every line is an exact command: nothing in it takes input
    from the account. With `aur` the pacman -U line lets the account install
    any package file it wrote, so any process running as that user can become
    root. That is the price of unattended AUR, and it is off unless asked for.
    """
    sh = sh or Shell()
    commands = [("true",), *(store.update for store in stores.STORES if store.root), repairs.PACCACHE, repairs.MKINITCPIO, repairs.DKMS]
    lines = [" ".join(argv) for argv in commands]
    if aur:  # what paru asks sudo for has not been observed, so no rule is guessed for it
        lines += [line.format(cache=aur_cache_root()) for line in stores.YAY_SUDO] if sh.has("yay") else []
    rules: list[str] = []
    skipped: list[str] = []
    for line in lines:
        program, _, arguments = line.partition(" ")
        path, reason = sh.root_binary(program)
        if path is None:
            skipped.append(f"# skipped {program}: {reason}")
        else:
            rules.append(f"{user} ALL=(root) NOPASSWD: {path} {arguments}".rstrip())
    header = [
        "# arch-update auto: generated by `arch-update auto --sudoers`",
        f"# Install as {SUDOERS_FILE}: sudo takes the LAST rule that matches, so this file",
        "# must be read after the one that lets you run everything with a password.",
    ]
    return "\n".join([*header, *dict.fromkeys(skipped), *rules]) + "\n"


def _packages_changed(before: str, after: str) -> int:
    """How many packages differ between two listings: a new version of one is one change, not two lines."""
    return len({line.split()[0] for line in set(before.splitlines()) ^ set(after.splitlines()) if line.strip()})


def _trouble(world: World) -> list[dict[str, Any]]:
    """Everything still wrong at the end, each with its census case and the one command for a human."""
    items = list(world.failed.values())
    if world.blocker and world.blocker not in items:
        items.append(world.blocker)
    if world.interrupted:
        by = "Ctrl-C" if world.signum == signal.SIGINT else signal.Signals(world.signum).name
        items.append(Verdict("run", "fail", "INTERRUPTED", f"stopped with {by}" if by == "Ctrl-C" else f"stopped by {by}"))
    known = {case.id: case for case in world.cases}
    return [
        {**asdict(item), "meaning": known[item.case].meaning if item.case in known else "", "fix": known[item.case].fix if item.case in known else ""}
        for item in items
    ]


def announce(sh: Shell, report: dict[str, Any]) -> None:
    """Tell the desktop how the run ended; a run nobody hears about is not finished."""
    if not sh.has("notify-send"):
        return
    body = report["crash"] or "; ".join(f"{item['probe']}: {cause(item['detail'])} → {item['fix']}" for item in report["trouble"])
    body = body or f"updated: {report['updated'] or 'nothing'}"
    if report["held"]:
        body += f" · held: {', '.join(report['held'])}"
    quiet = report["exit_code"] in (exits.IDLE, exits.READY_FOR_REBOOT, exits.SKIPPED) or exits.interrupted(report["exit_code"])
    urgency = "normal" if quiet else "critical"
    notice = ["notify-send", f"--urgency={urgency}", f"arch-update auto: {report['state']}", body[:400]]
    fix = next((item["fix"] for item in report["trouble"] if item["fix"]), AUR_BY_HAND if "aur" in report["held"] else "")
    terminal = sh.terminal()
    if not (fix and terminal and sh.has("systemd-run")):
        sh.capture(notice, timeout=10)
        return
    # A click on the notice opens a terminal running the one command that fixes what hangs.
    # The wait for that click lives in its own unit: this run does not stay alive for it.
    unit =["systemd-run", "--user", "--collect", "--quiet", f"--setenv=FIX={fix}", f"--setenv=OPEN={' '.join(terminal)}"]
    sh.capture([*unit, "--", "sh", "-c", CLICK_SCRIPT, "sh", *notice[:2], "--wait", f"--action=default={fix}", *notice[2:]], timeout=10)


def _new_run_dir(run_id: str) -> tuple[str, Path]:
    (STATE_ROOT / "runs").mkdir(parents=True, exist_ok=True)
    serial = 0
    while True:
        name = f"{run_id}-{serial}" if serial else run_id
        try:
            (STATE_ROOT / "runs" / name).mkdir()
        except FileExistsError:
            serial += 1
            continue
        return name, STATE_ROOT / "runs" / name


def look(sh: Shell | None = None) -> list[dict[str, Any]]:
    """One look at the machine, kept in memory: no run folder, no report, no lock and no vulnerability scan.

    What Update Day shows before anything is asked. A run's folder is made only when the update really runs.
    """
    world = World(sh or Shell(), load_json(CONFIG_PATH) if CONFIG_PATH.is_file() else None, [], "look", None, dry_run=True)
    return [asdict(verdict) for verdict in world.look()]


def run_auto(
    dry_run: bool = False,
    sh: Shell | None = None,
    watcher: Callable[[str, dict[str, Any]], None] | None = None,
    step: tis.Step | None = None,
) -> dict[str, Any]:
    """Run control.tis once against this machine and write the evidence.

    `watcher` is called with every event the run notes, and `step` before every
    instruction a node executes: together they are enough to draw the run live.
    """
    sh = sh or Shell()
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().astimezone().strftime("auto-%Y%m%dT%H%M%S%z")
    with (STATE_ROOT / "auto.lock").open("a", encoding="utf-8") as lock:
        try:
            # A dry run changes nothing, so it takes no lock and cannot make the timer skip its week.
            if not dry_run:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Every key a reader of a report reads, so the screen and `status` never meet a half-filled one.
            skipped = {"run_id": run_id, "started_at": None, "finished_at": datetime.now().astimezone().isoformat(), "dry_run": False, "out": 0}
            skipped.update(exit_code=exits.SKIPPED, state=exits.NAMES[exits.SKIPPED], looks=0, crash=None, trouble=[], verdicts=[], updated={}, held={})
            skipped.update(signal=None, child_stopped=None)
            skipped.update(repairs=[], pacnew=[], reboot_required=False, cve={"known": False, "error": "not scanned"}, wrote=None, reason="another run holds the lock")
            announce(sh, skipped)
            return skipped
        run_id, run_dir = _new_run_dir(run_id)
        started = datetime.now().astimezone().isoformat()
        world = World(sh, None, [], run_id, run_dir, dry_run=dry_run)
        world.watcher = watcher or world.watcher
        out, crash, previous, cve = 0, None, None, {"known": False, "error": "not scanned"}
        try:
            # On SIGTERM the command in flight is left to finish and no new one starts; the run then ends as interrupted.
            previous = signal.signal(signal.SIGTERM, lambda number, _frame: (setattr(world, "stop", True), setattr(world, "signum", number)))
            world.cases = census.load(STATE_ROOT)
            world.config = load_json(CONFIG_PATH) if CONFIG_PATH.is_file() else None
            out = tis.run(tis.parse(PROGRAM.read_text(encoding="utf-8")), world, halt="OUT", trace=world.trace, step=step)
            if world.dirty:
                world.look()  # halted after changing something: describe what was left, decide nothing
            cve = cve_axis()
        except KeyboardInterrupt as stop:  # the report below is still written and the lock still released
            out, world.interrupted = 0, True
            world.signum = getattr(stop, "signum", signal.SIGINT)
            world.child_stopped = getattr(stop, "child_stopped", None)
        except Exception as exc:  # whatever broke, the week ends in a report, not a traceback
            out, crash = 0, f"{type(exc).__name__}: {exc}"
        code = 128 + world.signum if world.interrupted else exits.TOOL_FAILURE if crash else exit_code(out, world)
        report = {
            "run_id": run_id,
            "started_at": started,
            "finished_at": datetime.now().astimezone().isoformat(),
            "dry_run": dry_run,
            "out": out,
            "exit_code": code,
            "signal": world.signum if world.interrupted else None,  # which one ended the run
            "child_stopped": world.child_stopped,  # did a signal end the command that was running (None: none was)
            "state": "DRY_RUN" if dry_run else exits.NAMES[code],
            "looks": world.looks,
            "crash": crash,
            "trouble": _trouble(world) if out <= 0 else [],
            "verdicts": [asdict(verdict) for verdict in world.verdicts],
            "updated": world.updated,
            "held": world.held,
            "repairs": world.repairs,
            "pacnew": world.pacnew,
            "reboot_required": world.reboot,
            "cve": cve,
            "wrote": str(run_dir),  # a dry run still keeps its report and lock file here
        }
        try:
            _write_json(run_dir / "report.json", report)
            if not dry_run:
                _write_json(STATE_ROOT / "last-auto.json", report)
        except OSError as exc:
            report.update(exit_code=exits.TOOL_FAILURE, state=exits.NAMES[exits.TOOL_FAILURE], crash=f"report not written: {exc}")
        finally:
            if not dry_run:
                announce(sh, report)
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
        return report


def status() -> str:
    """What still hangs from the last run, one line each, with the command that fixes it."""
    path = STATE_ROOT / "last-auto.json"
    try:
        report = load_json(path)
    except (OSError, ValueError):
        return "no unattended run on record yet\n"
    lines = [f"{report['state']} · {report['finished_at'][:16]} · updated {report['updated'] or 'nothing'}"]
    if report.get("crash"):
        lines.append(f"  crash: {report['crash']}")
    lines += [f"  {item['probe']}: {item['case']} — {cause(item['detail']) or item['meaning']}\n    fix: {item['fix']}" for item in report["trouble"]]
    lines += [f"  held {name}: {why if isinstance(why, str) else ', '.join(why)}" for name, why in report["held"].items()]
    lines += [f"  pacnew: {path}\n    fix: sudo pacdiff" for path in report["pacnew"]]
    if report["reboot_required"]:
        lines.append("  reboot pending\n    fix: systemctl reboot")
    return clean("\n".join(lines) + "\n")


def init_config() -> str:
    """Write the starter config once; an existing one is never touched."""
    if CONFIG_PATH.is_file():
        return f"{CONFIG_PATH} already exists\n"
    _write_json(CONFIG_PATH, STARTER_CONFIG)
    return f"wrote {CONFIG_PATH}\n"
