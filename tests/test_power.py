"""A power-off, a suspend or an empty battery in the middle of a transaction: the lock and the battery rule.

logind is scripted (a recorder in place of power.start) and the battery is a folder of files like /sys/class/power_supply,
as in test_quiet. The helper that takes the real lock is tested against a script that stands in for systemd-inhibit.
"""

from __future__ import annotations

import io
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from arch_updater import auto, briefs, census, engine, exits, power, watch
from arch_updater import main as cli
from arch_updater.power import Inhibitor, profile as real_profile, start as real_start  # noqa: F401 - bound before the suite's autouse fixture replaces them
from test_auto import UPGRADE, box, cases_of, ok  # noqa: F401 - box is a fixture
from test_kernel import _calls_recorder
from test_rollback import Tty, ground  # noqa: F401 - ground is a fixture


class Logind:
    """What systemd-logind would hold: the lock is open between start() and release()."""

    def __init__(self, error: str = "", fail_from: int | None = None) -> None:
        self.error, self.opened, self.open, self.whys = error, 0, False, []
        self.fail_from = fail_from  # the n-th start and the ones after it are refused
        self.locks: list = []
        self.seen: list[tuple[list[str], bool]] = []  # every command that changed the machine, and whether the lock was open then

    def start(self, why, wait=10.0):
        self.opened += 1
        self.whys.append(why)
        if self.error or (self.fail_from is not None and self.opened >= self.fail_from):
            return Inhibitor(error=self.error or "systemd-inhibit could not take the lock (Access denied)")
        self.open = True
        logind = self

        class Held(Inhibitor):
            dead = False

            @property
            def held(self) -> bool:
                return not self.dead

            def release(self) -> None:
                logind.open = False

        lock = Held()
        self.locks.append(lock)
        return lock

    def die(self) -> None:
        """The helper process is gone (killed, crashed): logind let the lock go."""
        self.open = False
        for lock in self.locks:
            lock.dead = True

    def watch(self, sh) -> None:
        """Make a scripted Shell note, for each command it runs, whether the lock was open."""
        for name, position in (("logged", 0), ("interactive", 0)):
            inner = getattr(sh, name)

            def noting(argv, *args, _inner=inner, **kwargs):
                self.seen.append((list(argv), self.open))
                return _inner(argv, *args, **kwargs)

            setattr(sh, name, noting)

    def transactions(self) -> list[tuple[list[str], bool]]:
        """The package and boot commands among the ones seen (a test's own list: the code under test guesses nothing)."""
        tools = {"pacman", "yay", "paru", "paccache", "mkinitcpio", "dkms", "snap", "flatpak", "grub-mkconfig"}
        return [(argv, held) for argv, held in self.seen if tools & {Path(word).name for word in argv}]


@pytest.fixture
def logind(monkeypatch):
    scripted = Logind()
    monkeypatch.setattr(power, "start", scripted.start)
    return scripted


def supplies(root: Path, battery: tuple[str, int] | None = None, mains: bool | None = None, extra: dict[str, tuple[str, str, str, int]] | None = None) -> None:
    """The machine's power supplies as /sys/class/power_supply shows them. `root` is the folder the suite put in place of /sys."""
    folder = root / "sys" / "class" / "power_supply"
    folder.mkdir(parents=True, exist_ok=True)

    def add(name, kind, status=None, capacity=None, online=None, scope=None):
        (folder / name).mkdir(exist_ok=True)
        (folder / name / "type").write_text(kind + "\n")
        for key, value in (("status", status), ("capacity", capacity), ("online", online), ("scope", scope)):
            if value is not None:
                (folder / name / key).write_text(f"{value}\n")

    if battery:
        add("BAT0", "Battery", status=battery[0], capacity=battery[1], scope="System")
    if mains is not None:
        add("AC", "Mains", online=int(mains))
    for name, (kind, status, scope, capacity) in (extra or {}).items():
        add(name, kind, status=status, capacity=capacity, scope=scope)


# ── the lock around every transaction ──────────────────────────────────────


def test_the_store_update_runs_under_the_lock_and_the_lock_is_let_go_after(box, logind) -> None:
    logind.watch(box.sh)
    report = box.run()
    assert report["updated"] == {"repo": 1} and logind.opened == 1
    ran = logind.transactions()
    assert (UPGRADE, True) in ran and any(argv[0] == "yay" for argv, _ in ran), "the repositories and the AUR"
    assert all(held for _, held in ran) and logind.open is False, "every one under the lock, and it ends with the run"


def test_it_is_one_lock_for_the_whole_run_and_it_is_not_taken_by_a_run_that_changes_nothing(box, logind) -> None:
    logind.watch(box.sh)
    box.sh.tools |= {"flatpak", "snap"}
    report = box.run()
    assert set(report["updated"]) == {"repo"} and logind.opened == 1
    assert all(held for argv, held in logind.transactions()) and len(logind.transactions()) >= 3
    logind.opened = 0
    box.run(dry_run=True)
    assert logind.opened == 0, "a dry run starts no transaction and takes no lock"


@pytest.mark.parametrize(
    ("mode", "message", "command"),
    [
        ("KERNEL-BOOT-ARTIFACT-MISSING", "kernel boot artifact missing: linux", ["sudo", "-n", "mkinitcpio", "-P"]),
        ("DKMS-PARTIAL-MODULE-FAILURE", "dkms install exited 10", ["sudo", "-n", "dkms", "autoinstall"]),
    ],
)
def test_a_repair_runs_under_the_lock_too(box, logind, mode, message, command) -> None:
    logind.watch(box.sh)
    box.issues.append({"mode": mode, "message": message, "reboot_required": False})
    ran = box.sh.logged
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: (box.issues.clear(), ran(argv, log, timeout))[1])
    report = box.run()
    assert report["repairs"][0]["attempted"] is True
    assert (command, True) in logind.transactions() and all(held for _, held in logind.transactions())


def test_the_cache_trim_runs_under_the_lock(box, logind) -> None:
    logind.watch(box.sh)
    box.sh.free = 1024**3
    box.run()
    assert logind.transactions() == [(["sudo", "-n", "paccache", "-rk2"], True)] * 3


def test_a_command_learned_from_a_failure_that_runs_pacman_is_a_transaction(box, logind) -> None:
    logind.watch(box.sh)
    world = auto.World(box.sh, None, [], "auto-x", box.root)
    world.command(["env", "-C", "/", "sh", "-c", "sudo pacman -S --needed foo && mkinitcpio -P"])
    world.command(["tar", "--zstd", "-cf", "/tmp/x", "/etc"])
    assert logind.seen[0][1] is True and logind.seen[1] == (["tar", "--zstd", "-cf", "/tmp/x", "/etc"], True), "taken before the first, kept after"
    assert logind.opened == 1
    world.release_power()
    assert logind.open is False


def test_update_day_y_runs_the_update_under_the_lock(box, logind) -> None:
    from arch_updater import day

    logind.watch(box.sh)
    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok("kwin 6.7.5-1.1 -> 6.7.5-3.1\n"), ok("")]
    assert day.ask(box.sh, io.StringIO("y\n\n"), io.StringIO(), look_only=False) == 0
    assert (UPGRADE, True) in logind.transactions() and all(held for _, held in logind.transactions())


# ── kernel, rollback, learn ────────────────────────────────────────────────


def test_the_kernel_install_and_every_grub_step_run_under_one_lock(monkeypatch, tmp_path, logind) -> None:
    calls = _calls_recorder(monkeypatch, tmp_path)
    inner = engine.stream_command
    monkeypatch.setattr(engine, "stream_command", lambda argv, log, dry_run=False: (logind.seen.append((list(argv), logind.open)), inner(argv, log, dry_run))[1])
    engine.select_kernel("lts")
    assert [argv[1] for argv, _ in logind.seen] == ["pacman", "cp", "install", "grub-mkconfig"] == [argv[1] for argv in calls]
    assert all(held for _, held in logind.seen) and logind.opened == 1 and logind.open is False


def test_a_failed_grub_step_is_rolled_back_under_the_lock(monkeypatch, tmp_path, logind) -> None:
    _calls_recorder(monkeypatch, tmp_path, failing=("install",))
    inner = engine.stream_command
    monkeypatch.setattr(engine, "stream_command", lambda argv, log, dry_run=False: (logind.seen.append((list(argv), logind.open)), inner(argv, log, dry_run))[1])
    with pytest.raises(RuntimeError, match="rolled back"):
        engine.select_kernel("lts")
    assert [argv[1] for argv, _ in logind.seen].count("cp") == 2 and all(held for _, held in logind.seen) and logind.open is False


def test_a_dry_run_of_the_kernel_takes_no_lock(monkeypatch, tmp_path, logind) -> None:
    _calls_recorder(monkeypatch, tmp_path)
    engine.select_kernel("lts", dry_run=True)
    assert logind.opened == 0


def test_rollback_installs_under_the_lock(ground, logind) -> None:
    logind.watch(ground.sh)
    from arch_updater import rollback

    assert rollback.run(ground.sh, ground.keep, stdin=Tty("y\n"), out=Tty()) == 0
    assert [(argv[:3], held) for argv, held in logind.seen] == [(["sudo", "pacman", "-U"], True)] and logind.open is False


def learned(tmp_path, monkeypatch, command, run, store="repo", where=""):
    """arch-update learn on a brief for `store`, with `run` in place of the real command runner and no retry."""
    from arch_updater import auto as auto_module

    monkeypatch.setattr(auto_module, "retry_for", lambda *a, **k: None)
    folder = briefs.write(store, census.Case(id="LOCAL-1"), "error: x\n", "auto-test", None, where, state_root=tmp_path)
    return briefs.learn(folder, "error: x", "how", command, state_root=tmp_path, run=run, followed=set())


def test_the_saved_fix_is_run_under_the_lock_whatever_it_is_called(tmp_path, monkeypatch, logind) -> None:
    ran = []
    # Nothing here names a package tool: a classifier would let this one through unlocked.
    learned(tmp_path, monkeypatch, "./fix-it.sh", lambda argv: (ran.append((argv[-1], logind.open)), (0, ""))[1])
    assert ran == [("./fix-it.sh", True)] and logind.opened == 1 and logind.open is False


def test_the_fix_that_learn_proves_in_a_throwaway_worktree_is_under_the_lock_too(tmp_path, monkeypatch, logind) -> None:
    seen = []
    monkeypatch.setattr(briefs, "_reproduce_merge", lambda *args: (seen.append(logind.open), True)[1])
    folder = briefs.write("dotfiles:x", census.Case(id="DOTFILES-CONFLICT"), "CONFLICT x\n", "auto-test", None, str(tmp_path), state_root=tmp_path, facts={"target": "v2", "rollback": "arch-update/before-x"})
    briefs.learn(folder, "CONFLICT", "how", "pacman -S --needed foo && git add -A", state_root=tmp_path, followed={str(tmp_path)})
    assert seen == [True] and logind.open is False


def test_learn_on_a_low_battery_runs_nothing_and_saves_nothing(tmp_path, monkeypatch, logind) -> None:
    supplies(tmp_path, battery=("Discharging", 11), mains=False)
    with pytest.raises(power.Refused, match="LOW-BATTERY"):
        learned(tmp_path, monkeypatch, "./fix-it.sh", lambda argv: pytest.fail("the fix was started"))
    assert not (tmp_path / census.LOCAL_FILE).exists() and logind.opened == 0


def test_the_throwaway_worktree_fix_is_not_run_on_a_low_battery(tmp_path, monkeypatch, logind) -> None:
    supplies(tmp_path, battery=("Discharging", 11), mains=False)
    monkeypatch.setattr(briefs, "_reproduce_merge", lambda *args: pytest.fail("the fix was started"))
    folder = briefs.write("dotfiles:x", census.Case(id="DOTFILES-CONFLICT"), "CONFLICT x\n", "auto-test", None, str(tmp_path), state_root=tmp_path, facts={"target": "v2", "rollback": "arch-update/before-x"})
    with pytest.raises(power.Refused, match="LOW-BATTERY"):
        briefs.learn(folder, "CONFLICT", "how", "pacman -S foo", state_root=tmp_path, followed={str(tmp_path)})


def test_a_lock_lost_between_the_fix_and_the_save_is_noticed_by_learn(tmp_path, monkeypatch, logind) -> None:
    supplies(tmp_path, battery=("Discharging", 80), mains=False)
    logind.fail_from = 2
    with pytest.raises(power.Refused, match="NO-INHIBITOR.*lost"):
        learned(tmp_path, monkeypatch, "./fix-it.sh", lambda argv: (logind.die(), (0, ""))[1])
    assert not (tmp_path / census.LOCAL_FILE).exists(), "nothing saved after the refusal"


# ── the battery ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("states", [{"battery": ("Discharging", 12), "mains": False}, {"battery": ("Discharging", 29)}])
def test_a_discharging_battery_under_the_limit_stops_the_run_before_anything_is_changed(box, logind, states) -> None:
    supplies(box.root, **states)
    report = box.run()
    assert report["state"] == "BLOCKED" and report["exit_code"] == exits.BLOCKED
    assert cases_of(report) == [("power", "LOW-BATTERY")]
    assert "battery" in report["trouble"][0]["detail"] and "%" in report["trouble"][0]["detail"] and report["trouble"][0]["fix"]
    assert box.sh.ran == [] and logind.opened == 0


def test_the_limit_is_a_key_and_exactly_at_it_the_run_goes(box, logind) -> None:
    supplies(box.root, battery=("Discharging", 30), mains=False)
    assert box.run()["updated"] == {"repo": 1}
    box.sh.packages[:] = ["a 1"]
    box.config(min_battery=50)
    assert cases_of(box.run()) == [("power", "LOW-BATTERY")]
    box.config(min_battery=0)
    supplies(box.root, battery=("Discharging", 3), mains=False)
    assert box.run()["updated"] == {"repo": 1}, "0 turns the rule off"


@pytest.mark.parametrize("nonsense", ["lots", -5, 250, True, None, 12.5])
def test_a_limit_that_makes_no_sense_is_the_default(nonsense) -> None:
    assert power.threshold({"min_battery": nonsense}) == 30 and power.threshold({}) == 30 and power.threshold(None) == 30


def test_a_charger_on_means_a_low_battery_does_not_stop_the_run(box, logind) -> None:
    supplies(box.root, battery=("Charging", 4), mains=True)
    assert box.run()["updated"] == {"repo": 1} and logind.opened == 1, "it goes on, and under the lock"


def test_a_desktop_is_never_stopped(box, logind) -> None:
    assert not (box.root / "sys" / "class" / "power_supply").exists()
    assert box.run()["updated"] == {"repo": 1} and logind.opened == 1, "it goes on, and under the lock"
    supplies(box.root, mains=True)
    box.sh.packages[:] = ["a 1"]
    assert box.run()["updated"] == {"repo": 1} and logind.opened == 2


def test_a_mouse_or_a_headset_battery_is_not_the_machines(box, logind) -> None:
    supplies(box.root, extra={"hid-mouse": ("Battery", "Discharging", "Device", 2), "ups-like": ("Battery", "Discharging", "Device", 1)})
    assert power.battery(box.root / "sys") == power.Battery(False, False, None)
    assert box.run()["updated"] == {"repo": 1} and logind.opened == 1


def test_two_batteries_the_lowest_one_in_use_counts(box) -> None:
    supplies(box.root, battery=("Discharging", 60), extra={"BAT1": ("Battery", "Discharging", "System", 8)})
    assert power.battery(box.root / "sys") == power.Battery(True, True, 8)
    supplies(box.root, battery=("Discharging", 60), extra={"BAT1": ("Battery", "Not charging", "System", 8)})
    assert power.battery(box.root / "sys") == power.Battery(True, True, 60), "a battery that is not in use does not count"


def test_a_battery_that_drains_during_the_run_stops_the_next_store(box, logind, monkeypatch) -> None:
    box.sh.tools |= {"flatpak"}
    def draining(config, sys_root=None):  # the charge falls once the repositories are updated
        return None if UPGRADE not in box.sh.ran else "the battery is at 9% and discharging, under the 30% a package update needs to start; plug in the charger"

    monkeypatch.setattr(power, "low", draining)
    report = box.run()
    assert report["updated"] == {"repo": 1}
    failed_stores = [item for item in report["trouble"] if item["case"] == "LOW-BATTERY"]
    assert failed_stores and "9%" in failed_stores[0]["detail"]
    assert box.sh.count("flatpak") == 0, "the store after the drop was not started"


def test_a_repair_is_not_started_on_a_battery_that_fell_under_the_limit(box, logind, monkeypatch) -> None:
    world = auto.World(box.sh, None, [], "auto-x", box.root)
    monkeypatch.setattr(power, "low", lambda config, sys_root=None: "the battery is at 5%")
    result = world.command(["sudo", "-n", "paccache", "-rk2"])
    assert result.returncode == 75 and "not started" in result.output and box.sh.ran == [] and logind.opened == 0
    assert world.held["power"] == "the battery is at 5%"


def test_the_kernel_is_not_installed_on_a_low_battery(monkeypatch, tmp_path, logind, capsys) -> None:
    calls = _calls_recorder(monkeypatch, tmp_path)
    supplies(tmp_path, battery=("Discharging", 11), mains=False)
    with pytest.raises(power.Refused, match="LOW-BATTERY.*11%"):
        engine.select_kernel("lts")
    assert calls == [] and logind.opened == 0
    assert cli.main(["kernel", "lts", "--yes"]) == 1
    assert "arch-update: LOW-BATTERY" in capsys.readouterr().err and calls == []


def test_rollback_does_not_ask_and_does_not_install_on_a_low_battery(ground, logind, tmp_path) -> None:
    from arch_updater import rollback

    supplies(tmp_path, battery=("Discharging", 11), mains=False)
    out = Tty()
    assert rollback.run(ground.sh, ground.keep, yes=True, stdin=Tty(""), out=out) == 1
    assert "LOW-BATTERY" in out.getvalue() and "nothing was changed" in out.getvalue()
    assert ground.sh.typed == [] and logind.opened == 0


# ── no lock ────────────────────────────────────────────────────────────────


def test_on_battery_without_systemd_inhibit_nothing_is_started_and_the_run_says_so(box, logind) -> None:
    supplies(box.root, battery=("Discharging", 80), mains=False)
    box.sh.tools.discard("systemd-inhibit")
    report = box.run()
    assert cases_of(report) == [("power", "NO-INHIBITOR")] and report["exit_code"] == exits.BLOCKED
    assert "systemd-inhibit is not installed" in report["trouble"][0]["detail"] and "battery" in report["trouble"][0]["detail"]
    assert box.sh.ran == []


def test_on_battery_with_a_lock_that_cannot_be_taken_the_store_is_not_started(box, monkeypatch) -> None:
    refusing = Logind(error="systemd-inhibit could not take the lock (Access denied)")
    monkeypatch.setattr(power, "start", refusing.start)
    supplies(box.root, battery=("Discharging", 80), mains=False)
    report = box.run()
    assert box.sh.ran == [] and ("repo", "NO-INHIBITOR") in cases_of(report)
    assert "Access denied" in report["trouble"][0]["detail"]


def test_on_mains_without_systemd_inhibit_the_run_goes_on_and_the_report_has_a_note(box, logind) -> None:
    supplies(box.root, battery=("Full", 100), mains=True)
    box.sh.tools.discard("systemd-inhibit")
    report = box.run()
    assert report["updated"] == {"repo": 1} and report["exit_code"] == 0
    detail = next(verdict["detail"] for verdict in report["verdicts"] if verdict["probe"] == "power")
    assert "systemd-inhibit is not installed" in detail and "mains" in detail


def test_on_a_desktop_with_a_lock_that_fails_the_run_goes_on_and_says_so_in_held(box, monkeypatch) -> None:
    refusing = Logind(error="systemd-inhibit could not take the lock (no D-Bus)")
    monkeypatch.setattr(power, "start", refusing.start)
    report = box.run()
    assert report["updated"] == {"repo": 1}
    assert "no D-Bus" in report["held"]["power"] and "mains" in report["held"]["power"]
    assert "held power" in auto.status() and "no D-Bus" in auto.status()


def test_the_kernel_without_a_lock_is_refused_on_battery_and_goes_on_with_a_note_on_mains(monkeypatch, tmp_path) -> None:
    refusing = Logind(error="systemd-inhibit is not installed")
    monkeypatch.setattr(power, "start", refusing.start)
    calls = _calls_recorder(monkeypatch, tmp_path)
    supplies(tmp_path, battery=("Discharging", 80), mains=False)
    with pytest.raises(power.Refused, match="NO-INHIBITOR.*systemd-inhibit is not installed"):
        engine.select_kernel("lts")
    assert calls == []
    supplies(tmp_path, battery=("Full", 100), mains=True)
    result = engine.select_kernel("lts")
    assert result["changed"] is True and "mains" in result["shutdown_lock"] and len(calls) == 4


def test_rollback_without_a_lock_on_battery_changes_nothing(ground, monkeypatch, tmp_path) -> None:
    from arch_updater import rollback

    monkeypatch.setattr(power, "start", Logind(error="systemd-inhibit is not installed").start)
    supplies(tmp_path, battery=("Discharging", 80), mains=False)
    out = Tty()
    assert rollback.run(ground.sh, ground.keep, stdin=Tty("y\n"), out=out) == 1
    assert "NO-INHIBITOR" in out.getvalue() and "nothing was changed" in out.getvalue() and ground.sh.typed == []


# ── the helper that takes the real lock ────────────────────────────────────


def fake_inhibit(tmp_path: Path, monkeypatch, body: str) -> Path:
    folder = tmp_path / "fake-bin"
    folder.mkdir()
    script = folder / "systemd-inhibit"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return script


def test_the_helper_takes_a_block_lock_on_all_four_things_and_lets_go_when_released(tmp_path, monkeypatch) -> None:
    record = tmp_path / "argv"
    # What systemd-inhibit does: take the lock (here: write the arguments), then run the command that follows the options.
    fake_inhibit(tmp_path, monkeypatch, f'echo "$@" > {record}\nshift 4\nexec "$@"\n')
    lock = real_start("updating packages")
    try:
        assert lock.held and lock.error == ""
        said = record.read_text()
        for option in ("--what=shutdown:sleep:idle:handle-lid-switch", "--who=arch-update", "--why=updating packages", "--mode=block"):
            assert option in said
        helper = lock.process
        assert helper.poll() is None
    finally:
        lock.release()
    assert helper.poll() is not None and lock.held is False


def test_a_lock_that_logind_refuses_is_a_reason_not_a_crash(tmp_path, monkeypatch) -> None:
    fake_inhibit(tmp_path, monkeypatch, 'echo "Failed to inhibit: Access denied" >&2\nexit 1\n')
    lock = real_start("x")
    assert lock.held is False and "Access denied" in lock.error


def test_no_systemd_inhibit_at_all_is_a_reason_not_a_crash(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    lock = real_start("x")
    assert lock.held is False and lock.error == "systemd-inhibit is not installed"
    lock.release()


def test_a_helper_that_never_answers_is_killed_and_reported(tmp_path, monkeypatch) -> None:
    pidfile = tmp_path / "pid"
    fake_inhibit(tmp_path, monkeypatch, f"echo $$ > {pidfile}\nexec sleep 30\n")
    lock = real_start("x", wait=0.3)
    assert lock.held is False and "no answer" in lock.error
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the helper that never answered is still running")


def test_the_lock_goes_when_the_tool_dies(tmp_path, monkeypatch) -> None:
    """The helper reads a pipe from this process: when the process is gone, the pipe is closed and the helper ends."""
    fake_inhibit(tmp_path, monkeypatch, "shift 4\nexec \"$@\"\n")
    lock = real_start("x")
    helper = lock.process
    helper.stdin.close()  # what the kernel does to the pipe when this process is killed
    helper.wait(timeout=5)
    assert lock.held is False


def test_the_helper_is_in_a_session_of_its_own_so_ctrl_c_does_not_drop_the_lock(tmp_path, monkeypatch) -> None:
    fake_inhibit(tmp_path, monkeypatch, "shift 4\nexec \"$@\"\n")
    lock = real_start("x")
    try:
        assert os.getsid(lock.process.pid) == lock.process.pid != os.getsid(0)
    finally:
        lock.release()


def test_the_lock_is_the_sudoers_rules_business_in_no_way(tmp_path) -> None:
    """The commands are not wrapped, so the exact commands the rules name are the ones that run."""
    rules = auto.sudoers_rules("u", sh=SimpleNamespace(root_binary=lambda tool, dirs=(): (f"/usr/bin/{tool}", ""), has=lambda tool: True))
    assert "systemd-inhibit" not in rules


def test_the_catalogue_has_both_cases_with_a_fix_and_a_stage(tmp_path) -> None:
    cases = {case.id: case for case in census.load(tmp_path)}
    for name in ("LOW-BATTERY", "NO-INHIBITOR"):
        assert cases[name].fix and cases[name].repair is None and not cases[name].classify_output
    assert watch.probe_power in watch.PROBES and watch.probe_power in watch.GATES


def test_update_day_on_a_low_battery_says_so_and_changes_nothing(box, logind) -> None:
    from arch_updater import day

    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok("kwin 6.7.5-1.1 -> 6.7.5-3.1\n"), ok("")]
    supplies(box.root, battery=("Discharging", 9), mains=False)
    out = io.StringIO()
    code = day.ask(box.sh, io.StringIO("y\n\n"), out, look_only=False)
    assert box.sh.ran == [] and logind.opened == 0
    assert "battery is at 9%" in out.getvalue() and "plug in the charger" in out.getvalue()
    assert code == exits.BLOCKED, out.getvalue()[-600:]


# ── dotfiles installers, a lock lost mid-phase, a helper that stalls ───────


def dotfiles_world(box, monkeypatch, logind):
    """A run with one dotfiles repository whose update is its installer, `./install.sh`, a command that names no package tool."""
    from arch_updater import dotfiles

    where = str(box.root / "dots")
    box.config(dotfiles=[{"path": where, "install": "./install.sh"}])
    box.patch.setattr(box.sh, "capture", lambda argv, timeout=30: ok())
    ran = []

    def update(world, repo, fetched=None):
        ran.append(logind.open)
        world.command(["env", "-C", where, "./install.sh"])
        return dotfiles.Outcome(moved=True)

    monkeypatch.setattr(dotfiles, "update", update)
    monkeypatch.setattr(dotfiles, "due", lambda *a, **k: True)
    return ran


def test_an_installer_in_the_run_is_under_the_lock_even_when_no_store_ran(box, logind, monkeypatch) -> None:
    logind.watch(box.sh)
    box.sh.tools -= {"pacman", "yay"}
    ran = dotfiles_world(box, monkeypatch, logind)
    report = box.run()
    assert ran == [True] and logind.opened == 1 and logind.open is False
    assert any(argv[-1] == "./install.sh" and held for argv, held in logind.seen), report["trouble"]


def test_an_installer_is_not_run_after_the_stores_were_refused_for_battery(box, logind, monkeypatch) -> None:
    ran = dotfiles_world(box, monkeypatch, logind)
    monkeypatch.setattr(power, "low", lambda config, sys_root=None: "the battery is at 5%" if UPGRADE in box.sh.ran else None)  # the repositories are updated, then the charge is low
    report = box.run()
    assert ran == [] and not any(argv[-1] == "./install.sh" for argv in box.sh.ran)
    assert {item["probe"] for item in report["trouble"]} >= {"dotfiles:" + str(box.root / "dots")}
    assert {item["case"] for item in report["trouble"]} == {"LOW-BATTERY"} and UPGRADE in box.sh.ran


def test_arch_update_dotfiles_alone_takes_the_lock_and_asks_the_battery(box, logind, monkeypatch, capsys) -> None:
    logind.watch(box.sh)
    ran = dotfiles_world(box, monkeypatch, logind)
    assert auto.run_dotfiles(sh=box.sh) == 0 and ran == [True] and logind.open is False
    supplies(box.root, battery=("Discharging", 7), mains=False)
    ran.clear()
    assert auto.run_dotfiles(sh=box.sh) == 1 and ran == []
    assert "LOW-BATTERY" in capsys.readouterr().out


def test_a_helper_that_died_between_two_stores_is_replaced_before_the_next_command(box, logind) -> None:
    logind.watch(box.sh)
    ran = box.sh.logged
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: (ran(argv, log, timeout), logind.die() if argv == UPGRADE else None)[0])
    box.run()
    after = [held for argv, held in logind.transactions() if argv[0] == "yay"]
    assert logind.opened == 2 and after and all(after), "the AUR update found a new lock, not a dead one"


def test_a_helper_that_died_on_a_battery_with_no_way_to_take_another_stops_the_next_store(box, monkeypatch) -> None:
    logind = Logind(fail_from=2)
    monkeypatch.setattr(power, "start", logind.start)
    box.sh.tools.add("flatpak")  # the AUR does not build on a battery at all; flatpak is the next store
    supplies(box.root, battery=("Discharging", 80), mains=False)
    ran = box.sh.logged
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: (ran(argv, log, timeout), logind.die() if argv == UPGRADE else None)[0])
    report = box.run()
    assert box.sh.count("flatpak") == 0 and ("flatpak", "NO-INHIBITOR") in cases_of(report)


def kernel_with_lost_lock(monkeypatch, tmp_path, logind):
    calls = _calls_recorder(monkeypatch, tmp_path)
    inner = engine.stream_command
    monkeypatch.setattr(engine, "stream_command", lambda argv, log, dry_run=False: (inner(argv, log, dry_run), logind.die() if argv[1] == "pacman" else None)[0])
    return calls


def test_a_kernel_install_whose_lock_was_lost_takes_another_before_it_touches_grub(monkeypatch, tmp_path, logind) -> None:
    calls = kernel_with_lost_lock(monkeypatch, tmp_path, logind)
    engine.select_kernel("lts")
    assert logind.opened == 2 and len(calls) == 4


def test_a_kernel_install_whose_lock_was_lost_on_a_battery_leaves_grub_alone(monkeypatch, tmp_path) -> None:
    logind = Logind(fail_from=2)
    monkeypatch.setattr(power, "start", logind.start)
    calls = kernel_with_lost_lock(monkeypatch, tmp_path, logind)
    supplies(tmp_path, battery=("Discharging", 80), mains=False)
    with pytest.raises(power.Refused, match="linux-lts is installed, but .*lost.*GRUB was left untouched"):
        engine.select_kernel("lts")
    assert [argv[1] for argv in calls] == ["pacman"]


def test_a_helper_that_prints_half_a_line_does_not_hang_start(tmp_path, monkeypatch) -> None:
    pidfile = tmp_path / "pid"
    fake_inhibit(tmp_path, monkeypatch, f"echo $$ > {pidfile}\nprintf held\nexec sleep 30\n")
    began = time.monotonic()
    lock = real_start("x", wait=0.4)
    assert time.monotonic() - began < 5 and lock.held is False and "no answer" in lock.error
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    pytest.fail("the stalled helper is still running")


def test_a_battery_that_falls_in_the_middle_of_one_repository_does_not_cut_its_steps_in_half(box, logind, monkeypatch) -> None:
    from arch_updater import dotfiles

    where = str(box.root / "dots")
    box.config(dotfiles=[{"path": where, "install": "./install.sh"}])
    box.patch.setattr(box.sh, "capture", lambda argv, timeout=30: ok())
    steps = []

    def update(world, repo, fetched=None):
        for step in ("pull", "install", "link"):
            steps.append(step)
            world.command(["env", "-C", where, "./" + step])
            monkeypatch.setattr(power, "low", lambda config, sys_root=None: "the battery is at 5%")  # it falls after the first step
        return dotfiles.Outcome(moved=True)

    monkeypatch.setattr(dotfiles, "update", update)
    monkeypatch.setattr(dotfiles, "due", lambda *a, **k: True)
    box.run()
    ran = [argv[-1] for argv in box.sh.ran if argv[:2] == ["env", "-C"]]
    assert ran == ["./pull", "./install", "./link"]


# ── P3, P5: heavy work does not run on a battery at all ────────────────────

def fake_profile(tmp_path, monkeypatch, value: str) -> None:
    """A powerprofilesctl on PATH that answers `get`; the suite's own stub of power.profile is lifted for the test."""
    from arch_updater import power as module

    monkeypatch.setattr(module, "profile", real_profile)
    folder = tmp_path / "ppd"
    folder.mkdir()
    tool = folder / "powerprofilesctl"
    tool.write_text(f'#!/bin/sh\n[ "$1" = get ] && echo {value}\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")


def test_on_a_battery_at_90_percent_no_aur_build_is_started_and_the_report_says_why(box, logind) -> None:
    supplies(box.root, battery=("Discharging", 90), mains=False)
    report = box.run()
    assert box.sh.count("aur") == 0 and box.sh.count("repo") == 1, "the repository update is the light part and goes on"
    assert "battery (90%)" in report["held"]["power"] and "AUR" in report["held"]["power"]
    assert "battery" in report["held"]["aur"] and "yay -Sua" in report["held"]["aur"]
    assert report["trouble"] == []


def test_on_mains_the_aur_is_built(box, logind) -> None:
    supplies(box.root, battery=("Charging", 90), mains=True)
    box.run()
    assert box.sh.count("aur") == 1


def test_a_rebuild_against_new_libraries_waits_for_the_charger_too(box, logind) -> None:
    box.config(unattended_aur=True, smoke=[{"argv": ["example-app", "--version"], "rebuild": "example-app-git"}])
    box.sh.tools.add("example-app")
    box.sh.fail["example-app --version"] = [auto_failed("symbol lookup error")] * 9
    supplies(box.root, battery=("Discharging", 90), mains=False)
    report = box.run()
    assert not any(argv[0] == "yay" for argv in box.sh.ran), report["repairs"]


def auto_failed(output):
    from test_auto import failed

    return failed(output)


def test_the_power_saver_profile_holds_the_aur_build_back_on_mains(box, logind, tmp_path, monkeypatch) -> None:
    fake_profile(tmp_path, monkeypatch, "power-saver")
    report = box.run()
    assert box.sh.count("aur") == 0 and "power-saver" in report["held"]["power"]


@pytest.mark.parametrize("value", ["balanced", "performance"])
def test_another_profile_does_not_hold_it_back(box, logind, tmp_path, monkeypatch, value) -> None:
    fake_profile(tmp_path, monkeypatch, value)
    box.run()
    assert box.sh.count("aur") == 1


def test_the_firmware_low_power_profile_counts_as_power_saver(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(power, "profile", real_profile)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no powerprofilesctl
    (tmp_path / "sys" / "firmware" / "acpi").mkdir(parents=True)
    (tmp_path / "sys" / "firmware" / "acpi" / "platform_profile").write_text("low-power\n")
    assert power.heavy_hold(tmp_path / "sys") and "power-saver" in power.heavy_hold(tmp_path / "sys")
    (tmp_path / "sys" / "firmware" / "acpi" / "platform_profile").write_text("balanced\n")
    assert power.heavy_hold(tmp_path / "sys") is None


def test_a_desktop_and_a_broken_reading_never_hold_work_back(tmp_path, monkeypatch) -> None:
    assert power.heavy_hold(tmp_path) is None
    monkeypatch.setattr(power, "battery", lambda root=None: 1 / 0)
    assert power.heavy_hold(tmp_path) is None


def test_scan_sessions_is_not_run_on_a_battery_whatever_the_charge(tmp_path, monkeypatch, capsys) -> None:
    from arch_updater import main as module

    scanned = []
    monkeypatch.setattr(module, "write_session_report", lambda out: scanned.append(out) or {"markdown": str(tmp_path / "r.md"), "report": {"counts": {"sessions_with_proven_changes": 0}}})
    (tmp_path / "r.md").write_text("report\n")
    supplies(tmp_path, battery=("Discharging", 95), mains=False)
    assert cli.main(["scan-sessions", "--output-dir", str(tmp_path)]) == 0
    assert scanned == [] and "scan-sessions was not run" in capsys.readouterr().err
    supplies(tmp_path, battery=("Charging", 95), mains=True)
    assert cli.main(["scan-sessions", "--output-dir", str(tmp_path)]) == 0 and len(scanned) == 1


# ── P4: the timer waits for the charger, a person is asked ─────────────────

@pytest.mark.parametrize("charge", [95, 35, 31])
def test_the_timer_on_a_battery_waits_for_the_charger_not_for_the_threshold(box, logind, capsys, charge) -> None:
    from arch_updater import day

    supplies(box.root, battery=("Discharging", charge), mains=False)
    assert day.scheduled(box.sh) == exits.SKIPPED
    assert box.sh.ran == [] and logind.opened == 0
    assert "waits for the charger" in capsys.readouterr().out


def test_the_timer_never_goes_onto_a_battery_when_the_wait_is_long_but_tells_the_person(box, logind, capsys) -> None:
    from arch_updater import day, quiet

    supplies(box.root, battery=("Discharging", 95), mains=False)
    (day.STATE_ROOT).mkdir(parents=True, exist_ok=True)
    (day.STATE_ROOT / quiet.WAITING).write_text(json.dumps({"since": time.time() - (quiet.MAX_WAIT_DAYS + 1) * 86400, "reason": "on battery"}))
    assert day.scheduled(box.sh) == exits.SKIPPED
    assert box.sh.ran == [] and any(call[0] == "notify-send" for call in box.sh.calls + box.sh.ran), "the person is told"
    assert "waits for the charger" in capsys.readouterr().err


def test_the_timer_on_mains_updates(box, logind) -> None:
    from arch_updater import day

    supplies(box.root, battery=("Charging", 50), mains=True)
    assert day.scheduled(box.sh) == 0 and box.sh.ran.count(UPGRADE) == 1


def test_update_day_asks_on_a_battery_in_its_own_frame(box, logind) -> None:
    from arch_updater import day

    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok("kwin 6.7.5-1.1 -> 6.7.5-3.1\n"), ok("")]
    supplies(box.root, battery=("Discharging", 72), mains=False)
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("n\n"), out, look_only=False)
    assert "ON BATTERY, 72%. CONTINUE?" in out.getvalue() and "UPDATE NOW?" not in out.getvalue() and box.sh.ran == []


def test_update_day_on_mains_asks_the_plain_question(box, logind) -> None:
    from arch_updater import day

    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok("kwin 6.7.5-1.1 -> 6.7.5-3.1\n"), ok("")]
    supplies(box.root, battery=("Charging", 72), mains=True)
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("n\n"), out, look_only=False)
    assert "UPDATE NOW?" in out.getvalue() and "ON BATTERY" not in out.getvalue()


def test_arch_update_auto_by_hand_on_a_battery_asks_and_a_no_runs_nothing(box, logind, monkeypatch, capsys) -> None:
    ran = []
    monkeypatch.setattr(cli, "run_auto", lambda **kwargs: ran.append(kwargs) or {"exit_code": 0, "wrote": "x"})

    class Typed(io.StringIO):
        def isatty(self):
            return True

    supplies(box.root, battery=("Discharging", 72), mains=False)
    monkeypatch.setattr("sys.stdin", Typed("n\n"))
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert cli.main(["auto"]) == 0 and ran == []
    assert "On battery, 72%. Continue?" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", Typed("y\n"))
    cli.main(["auto"])
    assert len(ran) == 1


# ── P6: a background job on a laptop ───────────────────────────────────────

def test_the_weekly_service_unit_is_lowest_priority_idle_io_and_at_most_half_a_core(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from arch_updater import scheduler

    monkeypatch.setattr(scheduler, "run_capture", lambda argv, **kw: SimpleNamespace(returncode=0, output=""))
    monkeypatch.setattr(scheduler.shutil, "which", lambda tool: "/opt/deck/bin/arch-update")
    scheduler.install_weekly_auto_timer(unit_dir=tmp_path)
    text = (tmp_path / "arch-update-auto.service").read_text()
    service = text.split("[Service]")[1]
    settings = dict(line.split("=", 1) for line in service.splitlines() if "=" in line and not line.startswith("#"))
    assert settings["Nice"] == "19" and settings["IOSchedulingClass"] == "idle"
    assert int(settings["CPUQuota"].rstrip("%")) <= 50


def test_on_a_battery_the_run_takes_at_most_half_a_core_and_nice_19(tmp_path, monkeypatch) -> None:
    from test_quiet import proc_with, sys_with

    from arch_updater import quiet

    root = sys_with(tmp_path, mains_online="0", smt_pairs=8)  # 8 cores: a quarter is 2, 60 % each, 120 % without a battery
    (root / "devices/system/cpu/online").write_text("0-15\n")
    (root / "class" / "power_supply" / "BAT0" / "status").write_text("Discharging\n")
    (root / "class" / "power_supply" / "BAT0" / "capacity").write_text("80\n")
    niced, calls = [], []
    monkeypatch.setattr(quiet.os, "nice", niced.append)
    unit = proc_with(tmp_path, cgroup="/user.slice/arch-update-auto.service")
    done = quiet.calm({}, lambda argv: calls.append(argv) or 0, unit, root)
    assert "CPUQuota=50%" in calls[1] and niced == [19] and "nice 19" in done
    (root / "class" / "power_supply" / "ACAD" / "online").write_text("1\n")
    (root / "class" / "power_supply" / "BAT0" / "status").write_text("Charging\n")
    calls.clear()
    quiet.calm({}, lambda argv: calls.append(argv) or 0, unit, root)
    assert "CPUQuota=120%" in calls[1]


# ── P7 with the real helper: its process is killed between two stores ──────

def killed_between_two_stores(box, monkeypatch, tmp_path, script: str, battery: bool):
    """The real power.start against a stand-in systemd-inhibit; the first helper is killed once pacman is done."""
    fake_inhibit(tmp_path, monkeypatch, script)
    started: list[Inhibitor] = []

    def start(why, wait=10.0):
        lock = real_start(why, wait)
        started.append(lock)
        return lock

    monkeypatch.setattr(power, "start", start)
    if battery:
        box.sh.tools.add("flatpak")
        supplies(box.root, battery=("Discharging", 80), mains=False)
    seen = []
    ran = box.sh.logged

    def logged(argv, log, timeout=0):
        result = ran(argv, log, timeout)
        if argv == UPGRADE:
            started[0].process.kill()  # the helper dies (OOM, a stray kill) with the first store done
            started[0].process.wait()
        elif argv[0] in ("yay", "flatpak"):
            seen.append((argv[0], started[-1].held, len(started)))
        return result

    monkeypatch.setattr(box.sh, "logged", logged)
    return started, seen


def test_a_helper_process_killed_between_two_stores_is_replaced_before_the_next_one(box, monkeypatch, tmp_path) -> None:
    started, seen = killed_between_two_stores(box, monkeypatch, tmp_path, 'shift 4\nexec "$@"\n', battery=False)
    box.run()
    assert seen == [("yay", True, 2)], "the AUR update ran under a second, living helper"


def test_a_helper_process_killed_between_two_stores_on_a_battery_that_cannot_be_replaced_stops_the_next_store(box, monkeypatch, tmp_path) -> None:
    once = tmp_path / "once"
    script = f'[ -e {once} ] && exit 1\ntouch {once}\nshift 4\nexec "$@"\n'
    started, seen = killed_between_two_stores(box, monkeypatch, tmp_path, script, battery=True)
    report = box.run()
    assert seen == [] and len(started) == 2 and ("flatpak", "NO-INHIBITOR") in cases_of(report)
