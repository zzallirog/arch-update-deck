"""The ports behind control.tis, run against a scripted machine.

The real probes, stores, repairs and census run; only the Shell is replaced.
"""

import fcntl
import itertools
import signal
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from arch_updater import auto, census, exits, repairs, scheduler, stores, watch
from arch_updater.shell import Shell
from arch_updater.textsafe import cause

UPGRADE = ["sudo", "-n", "pacman", "-Syu", "--noconfirm"]
NOT_FOUND = "error: failed retrieving file 'x' : The requested URL returned error: 404"
CONFLICT = "error: unresolvable package conflicts detected\n:: foo and bar are in conflict"
UNSEEN = "error: the flux capacitor at 0x7f3a returned 88 gigawatts"


def ok(output=""):
    return SimpleNamespace(returncode=0, output=output)


def failed(output, returncode=1):
    return SimpleNamespace(returncode=returncode, output=output)


def store_of(argv):
    """Which store's update a logged command is, or None."""
    words = [word for word in argv if word not in ("sudo", "-n")]
    update = {"pacman": "repo", "yay": "aur", "paru": "aur", "flatpak": "flatpak", "snap": "snap"}
    return update.get(words[0]) if words[1:2] != ["-S"] else None


class Machine(Shell):
    """A machine where everything works; a test breaks one thing."""

    def __init__(self, *tools):
        self.tools = {"pacman", "yay", "notify-send", *tools}
        self.packages = ["a 1"]
        self.fail: dict[str, list] = {}  # store or command name -> results that replace success
        self.ran: list[list[str]] = []  # commands that change the machine
        self.calls: list[list[str]] = []  # read-only commands
        self.naps: list[float] = []
        self.dns = True
        self.free = 100 * 1024**3
        self.aur = {"type": "multiinfo", "results": []}
        self.typed: list[list[str]] = []  # commands handed the person's own terminal (a password prompt)
        self.typed_status = 0

    def has(self, tool):
        return tool in self.tools

    def path(self, tool):
        return f"/usr/bin/{tool}"

    def root_binary(self, tool, dirs=()):
        return (f"/usr/bin/{tool}", "") if tool in self.tools else (None, f"{tool} not found")

    def capture(self, argv, timeout=30):
        self.calls.append(list(argv))
        scripted = self.fail.get(" ".join(argv[:2]))
        if scripted:
            return scripted.pop(0)
        if argv == ["pacman", "-Qqm"]:
            return ok("old\nnew\n")
        return ok("\n".join(self.packages) + "\n") if argv[:2] == ["pacman", "-Q"] else ok()

    def interactive(self, argv):
        self.typed.append(list(argv))
        return self.typed_status

    def logged(self, argv, log, timeout=0):
        self.ran.append(list(argv))
        scripted = self.fail.get(store_of(argv) or argv[0])
        if scripted:
            return scripted.pop(0)
        if argv == UPGRADE:
            self.packages[:] = ["a 2"]
        return ok()

    def resolves(self, host="archlinux.org"):
        return self.dns

    def free_bytes(self, path):
        return self.free

    def sleep(self, seconds):
        self.naps.append(seconds)

    def post(self, url, fields):
        if isinstance(self.aur, Exception):
            raise self.aur
        return self.aur

    def count(self, store):
        return sum(store_of(argv) == store for argv in self.ran)


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(scheduler, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(auto, "CONFIG_PATH", tmp_path / "auto.json")
    (tmp_path / "auto.json").write_text('{"conditions": [], "unattended_aur": true}')
    monkeypatch.setattr(repairs, "PACMAN_LOCK", tmp_path / "db.lck")
    monkeypatch.setattr(watch, "snapshot", lambda **kwargs: {"pacnew": []})
    monkeypatch.setattr(watch, "attest", lambda **kwargs: {"issues": box.issues})
    monkeypatch.setattr(watch.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(auto, "scan_cves", lambda: {"available": True, "findings": [{"severity": "High"}]})
    box = SimpleNamespace(sh=Machine(), root=tmp_path, patch=monkeypatch, issues=[])
    box.run = lambda **kwargs: auto.run_auto(sh=box.sh, **kwargs)
    box.config = lambda **keys: (tmp_path / "auto.json").write_text(json.dumps({"conditions": [], "unattended_aur": True, **keys}))
    return box


def cases_of(report):
    return [(item["probe"], item["case"]) for item in report["trouble"]]


# ── the census: cases that already happened ────────────────────────────────


def test_every_case_names_a_repair_that_exists_and_a_command_for_a_human(tmp_path) -> None:
    for case in census.load(tmp_path):
        assert case.fix, f"{case.id} has no command for a human"
        assert case.repair is None or case.repair in repairs.ACTIONS, f"{case.id} names an unknown repair"


def test_no_fix_is_the_command_that_just_failed(tmp_path) -> None:
    """A fix that repeats the store's own command is no way forward: the same update fails the same way."""
    commands = {" ".join(store.update[:2]) for store in stores.STORES} | {"yay -Sua", "arch-update auto"}
    for case in census.load(tmp_path):
        assert case.fix.removeprefix("sudo ") not in commands, f"{case.id}: its fix is the command that fails ({case.fix})"


def test_every_fix_is_one_printable_command_the_shell_can_read(tmp_path) -> None:
    for case in census.load(tmp_path):
        assert "\n" not in case.fix and "<" not in case.fix, f"{case.id}: one line, no placeholder"
        assert subprocess.run(["sh", "-n", "-c", case.fix], capture_output=True).returncode == 0, f"{case.id}: {case.fix}"


def test_the_sudoers_fix_installs_through_a_temporary_file_checked_by_visudo(tmp_path) -> None:
    for name in ("NO-ROOT", "SUDO-TIMESTAMP-EXPIRED"):
        fix = census.find(census.load(tmp_path), name).fix
        assert fix != "arch-update auto --sudoers", "that command only prints the rules"
        assert fix.index("mktemp") < fix.index("visudo -cf") < fix.index("sudo install -m 0440") and "/etc/sudoers.d/zzz-arch-update-auto" in fix
        assert "| sudo tee" not in fix, "rules that were never checked are not written to /etc"


def test_a_local_case_quotes_the_path_of_its_log(tmp_path) -> None:
    log = tmp_path / "a run" / "run.log"
    case = census.record([], tmp_path, UNSEEN, "repo", log)
    assert case.fix == f"less +G '{log}'"


@pytest.mark.parametrize(
    ("terminal", "flag"),
    [("gnome-terminal", "--wait"), ("konsole", "--nofork"), ("wezterm", "--always-new-process")],
)
def test_a_terminal_that_hands_its_window_to_a_server_is_told_to_wait(terminal, flag) -> None:
    """The weekly unit reads the answer when the window's process ends: a terminal that returns at once ends it early."""
    from arch_updater import shell

    assert flag in shell.TERMINALS[terminal]
    machine = Machine(terminal)
    machine.has = lambda tool: tool == terminal
    assert flag in machine.terminal(), "the command that opens the window carries it"


def test_the_low_space_case_describes_the_limit_and_the_cleanup_the_code_has(tmp_path) -> None:
    """`arch-update vault` prints this text: it must name the live threshold and command, not an old design."""
    from arch_updater import engine

    entry = next(m for m in json.loads(engine.VAULT_PATH.read_text())["modes"] if m["id"] == "ROOT-SPACE-LOW")
    response = entry["response"]
    assert f"{engine.SOFT_FREE_BYTES / 1024**3:g} GiB" in response and " ".join(repairs.PACCACHE) in response
    assert "-rk1" not in response and "4 GiB" not in response, "the interactive floor and the harder cleanup no longer exist"
    assert entry["fix"].startswith("sudo " + " ".join(repairs.PACCACHE))


def test_every_case_a_probe_can_name_is_in_the_census(tmp_path) -> None:
    known = {case.id for case in census.load(tmp_path)}
    source = (watch.__file__, auto.__file__)
    named = {word for path in source for word in __import__("re").findall(r'"([A-Z][A-Z-]+[A-Z])"', open(path).read())}
    assert named - {"WATCH", "APPLY", "CENSUS", "OUT", "DRY_RUN", "LOCAL-"} <= known


def test_a_failure_takes_a_repairable_case_only_when_every_cause_in_it_is_repairable(tmp_path) -> None:
    cases = census.load(tmp_path)
    assert census.recognise(cases, NOT_FOUND).id == "MIRROR-STALE-404"
    assert census.recognise(cases, NOT_FOUND + "\n" + CONFLICT).id == "PACMAN-REPLACEMENT-CONFLICT"
    # ROOT-SPACE-LOW is repairable and stands first in the census; the unrepairable cause must still win.
    assert census.recognise(cases, "error: no space left on device\n" + CONFLICT).id == "PACMAN-REPLACEMENT-CONFLICT"
    assert census.recognise(cases, "error: failed to synchronize: unable to lock database").id == "PACMAN-LOCK"
    assert census.recognise(cases, "curl: (6) Could not resolve host: mirror").id == "DNS-FAILED"
    assert census.recognise(cases, UNSEEN) is None


def test_a_token_is_the_cases_place_in_the_census(tmp_path) -> None:
    cases = census.load(tmp_path)
    for case in cases:
        assert census.at(cases, census.token(cases, case)) is case
    assert census.at(cases, 0) is None and census.at(cases, -999) is None and census.at(cases, 5) is None


def test_an_unseen_failure_becomes_a_case_and_is_known_the_next_time(box) -> None:
    box.sh.fail["repo"] = [failed(UNSEEN)]
    first = box.run()
    assert first["exit_code"] == exits.RECOVERY_PENDING
    (probe, case_id), = cases_of(first)
    assert probe == "repo" and case_id.startswith("LOCAL-")
    assert first["trouble"][0]["fix"].startswith("less +G ") and "88 gigawatts" in first["trouble"][0]["meaning"]
    box.sh.fail["repo"] = [failed(UNSEEN.replace("0x7f3a", "0x1b2c").replace("88", "121"))]
    second = box.run()
    assert cases_of(second) == [("repo", case_id)], "the same failure with other numbers is the same case"
    saved = json.loads((box.root / "census.json").read_text())
    assert [(case["id"], case["seen"]) for case in saved] == [(case_id, 2)]


def test_a_local_case_given_a_repair_is_reused(box) -> None:
    box.sh.fail["repo"] = [failed(UNSEEN)]
    case_id = cases_of(box.run())[0][1]
    saved = json.loads((box.root / "census.json").read_text())
    saved[0]["repair"] = "wait-for-mirror"
    (box.root / "census.json").write_text(json.dumps(saved))
    box.sh.fail["repo"] = [failed(UNSEEN)]
    report = box.run()
    assert report["state"] == "IDLE"
    assert report["repairs"] == [{"case": case_id, "repair": "wait-for-mirror", "attempted": True}]


# ── a look at the machine ──────────────────────────────────────────────────


def test_a_broken_probe_does_not_silence_the_others(box) -> None:
    def probe_broken(scene):
        raise TypeError("conditions is not a list")

    box.patch.setattr(watch, "PROBES", (probe_broken, *watch.PROBES))
    report = box.run(dry_run=True)
    statuses = {verdict["probe"]: verdict["status"] for verdict in report["verdicts"]}
    assert statuses.pop("broken") == "unknown"
    assert set(statuses) == {"privilege", "keep", "lock", "disk", "dns", "database", "machine", "smoke"}
    assert set(statuses.values()) == {"pass"}
    assert report["out"] == 0, "unknown is never green"


def test_probes_change_nothing_they_share(box) -> None:
    scene = watch.Scene(box.sh, {"smoke": [{"argv": ["qs", "--version"], "rebuild": "qs-git"}]}, box.root / "auto.json", box.root, None)
    box.sh.tools.add("qs")
    box.sh.fail["qs --version"] = [failed("symbol lookup error")]
    reading = watch.look(scene)
    assert reading.facts["rebuild"] == ["qs-git"] and scene.baseline is None
    assert [verdict.case for verdict in reading.verdicts if verdict.status == "fail"] == ["ABI-MISMATCH"]


def test_a_blocked_look_never_reaches_a_package_manager(box) -> None:
    box.config(conditions=[{"name": "home", "argv": ["at-home"], "exit": 40}])
    box.sh.fail["at-home"] = [failed("foreign segment")]
    report = box.run()
    assert report["state"] == "NETWORK_BLOCKED" and report["exit_code"] == 40
    assert cases_of(report) == [("home", "CONDITION")] and report["trouble"][0]["fix"]
    assert box.sh.ran == []


@pytest.mark.parametrize(("exit_asked", "exit_given"), [(40, 40), (50, 50), (20, 20), (0, 20), (10, 20), (70, 20), (None, 20)])
def test_a_condition_may_only_claim_a_blocking_exit(box, exit_asked, exit_given) -> None:
    condition = {"name": "guard", "argv": ["guard"]} | ({} if exit_asked is None else {"exit": exit_asked})
    box.config(conditions=[condition])
    box.sh.fail["guard"] = [failed("no")]
    assert box.run()["exit_code"] == exit_given


def test_a_machine_without_a_config_is_blocked_not_assumed_safe(box) -> None:
    (box.root / "auto.json").unlink()
    report = box.run()
    assert report["exit_code"] == exits.TOOL_FAILURE and cases_of(report) == [("conditions", "NOT-CONFIGURED")]
    assert report["trouble"][0]["fix"] == "arch-update auto --init" and box.sh.ran == []


def test_no_passwordless_root_is_said_before_anything_runs(box) -> None:
    box.sh.fail["sudo -n"] = [failed("sudo: a password is required")]
    report = box.run()
    assert report["exit_code"] == exits.TOOL_FAILURE and cases_of(report) == [("privilege", "NO-ROOT")]
    assert "arch-update auto --sudoers >" in report["trouble"][0]["fix"] and box.sh.ran == []


def test_a_pending_reboot_is_reported_but_does_not_stop_updates(box) -> None:
    box.issues.append({"mode": "RUNNING-KERNEL-MODULES-MISSING", "message": "gone", "reboot_required": True})
    report = box.run()
    assert report["state"] == "READY_FOR_REBOOT" and report["exit_code"] == 10
    assert report["updated"] == {"repo": 1} and report["reboot_required"] is True


def test_a_machine_failure_with_a_repair_on_record_is_repaired_and_looked_at_again(box) -> None:
    box.issues.append({"mode": "KERNEL-BOOT-ARTIFACT-MISSING", "message": "kernel boot artifact missing: linux", "reboot_required": False})
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: (box.sh.ran.append(argv), box.issues.clear(), ok())[-1])
    report = box.run()
    assert report["repairs"][0] == {"case": "KERNEL-BOOT-ARTIFACT-MISSING", "repair": "rebuild-initramfs", "attempted": True}
    assert box.sh.ran[0] == ["sudo", "-n", "mkinitcpio", "-P"] and report["state"] == "IDLE"


def test_a_machine_failure_nobody_can_repair_stops_the_run_with_its_fix(box) -> None:
    box.issues.append({"mode": "NEW-FAILED-UNIT", "message": "new failed unit (system): sddm.service", "reboot_required": False})
    report = box.run()
    assert report["exit_code"] == exits.RECOVERY_PENDING and cases_of(report) == [("machine", "NEW-FAILED-UNIT")]
    assert "systemctl --failed" in report["trouble"][0]["fix"] and box.sh.ran == []


def test_repairs_stop_at_three_a_run(box) -> None:
    (box.root / "db.lck").touch()
    report = box.run()
    assert [item["attempted"] for item in report["repairs"]] == [True, True, True]
    assert report["exit_code"] == exits.RECOVERY_PENDING and (box.root / "db.lck").exists()
    assert box.sh.naps == [5] * 72 and box.sh.ran == []


def test_low_disk_and_dead_dns_take_their_own_repairs(box) -> None:
    box.sh.free, box.sh.dns = 1024**3, False
    report = box.run()
    assert [item["case"] for item in report["repairs"]] == ["ROOT-SPACE-LOW"] * 3
    assert box.sh.ran == [["sudo", "-n", "paccache", "-rk2"]] * 3
    box.sh.free, box.sh.ran = 100 * 1024**3, []
    assert [item["case"] for item in box.run()["repairs"]] == ["DNS-FAILED"] * 3
    assert box.sh.ran == [["resolvectl", "flush-caches"]] * 3


def test_a_program_that_is_not_installed_is_not_broken(box) -> None:
    box.config(smoke=[{"argv": ["example-app", "--version"], "rebuild": "example-app-git"}])
    box.sh.fail["example-app --version"] = [failed("symbol lookup error")] * 9
    assert box.run()["state"] == "IDLE"
    box.sh.tools.add("example-app")
    box.sh.fail["example-app --version"] = [failed("symbol lookup error")]
    box.sh.ran.clear()
    report = box.run()
    assert report["repairs"][0]["case"] == "ABI-MISMATCH" and report["repairs"][0]["attempted"] is True
    rebuild = next(argv for argv in box.sh.ran if argv[:3] == ["yay", "-S", "--rebuild"])
    assert rebuild[-1] == "example-app-git" and box.sh.ran.index(rebuild) == 0, "repaired before any store ran"


@pytest.mark.parametrize(
    ("before", "after", "count"),
    [
        ("foo 1-1\nbar 2-1\n", "foo 1-2\nbar 2-1\n", 1),  # a new version of one package is one change
        ("foo 1-1\n", "foo 1-1\nnew 1-1\n", 1),  # a new package
        ("foo 1-1\ngone 1-1\n", "foo 1-1\n", 1),  # a removed one
        ("foo 1-1\nbar 2-1\n", "foo 1-2\nbar 2-2\nnew 1-1\n", 3),
        ("foo 1-1\n", "foo 1-1\n\n", 0),
    ],
)
def test_a_package_that_changed_version_counts_once(before, after, count) -> None:
    assert auto._packages_changed(before, after) == count


def test_a_rebuild_is_an_aur_build_and_obeys_the_aur_switch(box) -> None:
    box.config(unattended_aur=False, smoke=[{"argv": ["example-app", "--version"], "rebuild": "example-app-git"}])
    box.sh.tools.add("example-app")
    box.sh.fail["example-app --version"] = [failed("symbol lookup error")] * 9
    report = box.run()
    assert report["repairs"] == [{"case": "ABI-MISMATCH", "repair": "rebuild-against-new-libraries", "attempted": False}]
    assert not any(argv[0] == "yay" for argv in box.sh.ran)


# ── updating the stores ────────────────────────────────────────────────────


def test_a_clean_week_updates_once_and_settles(box) -> None:
    report = box.run()
    assert report["state"] == "IDLE" and report["exit_code"] == 0 and report["trouble"] == []
    assert report["updated"] == {"repo": 1} and report["looks"] == 2
    assert box.sh.count("repo") == 1 and box.sh.count("aur") == 1
    assert (box.root / "keep" / report["run_id"] / "packages.txt").read_text() == "a 1\n"
    assert report["cve"] == {"known": True, "findings": 1, "by_severity": {"High": 1}}


def test_a_week_with_nothing_new_still_runs_the_update(box) -> None:
    box.sh.packages[:] = ["a 2"]
    report = box.run()
    assert report["state"] == "IDLE" and report["updated"] == {} and report["looks"] == 1 and box.sh.count("repo") == 1


INSTALLED = [combo for size in range(4) for combo in itertools.combinations(("yay", "flatpak", "snap"), size)]
STORE_OF_TOOL = {"pacman": "repo", "yay": "aur", "flatpak": "flatpak", "snap": "snap"}
MATRIX = [
    (tools, failing, kind)
    for tools in INSTALLED
    for failing in (None, "repo", *(STORE_OF_TOOL[tool] for tool in tools))
    for kind in (("repairable", "unrepairable", "unseen") if failing else ("none",))
]


@pytest.mark.parametrize(("tools", "failing", "kind"), MATRIX, ids=lambda value: "+".join(value) if isinstance(value, tuple) else str(value))
def test_one_store_failing_never_stops_the_others(box, tools, failing, kind) -> None:
    box.sh.tools = {"pacman", "notify-send", *tools}
    present = ["repo", *(STORE_OF_TOOL[tool] for tool in tools)]
    output = {"repairable": NOT_FOUND, "unrepairable": CONFLICT, "unseen": UNSEEN, "none": ""}[kind]
    if failing:
        box.sh.fail[failing] = [failed(output)]
    report = box.run()
    for store in present:
        expected = 2 if (store == failing and kind == "repairable") else 1
        assert box.sh.count(store) == expected, f"{store} ran {box.sh.count(store)} times"
    for store in set(STORE_OF_TOOL.values()) - set(present):
        assert box.sh.count(store) == 0
    if kind in ("none", "repairable"):
        assert report["state"] == "IDLE" and report["trouble"] == []
    else:
        assert report["exit_code"] == exits.RECOVERY_PENDING
        (probe, case_id), = cases_of(report)
        assert probe == failing and report["trouble"][0]["fix"]
        assert case_id == "PACMAN-REPLACEMENT-CONFLICT" if kind == "unrepairable" else case_id.startswith("LOCAL-")
    if kind == "repairable":
        assert report["repairs"] == [{"case": "MIRROR-STALE-404", "repair": "wait-for-mirror", "attempted": True}]


def test_two_stores_failing_are_both_on_record(box) -> None:
    box.sh.tools.add("flatpak")
    box.sh.fail["repo"], box.sh.fail["flatpak"] = [failed(CONFLICT)], [failed(UNSEEN)]
    report = box.run()
    assert [probe for probe, _ in cases_of(report)] == ["repo", "flatpak"] and box.sh.count("aur") == 1


def test_the_census_is_asked_about_the_failure_it_can_repair_even_when_another_came_first(box) -> None:
    box.sh.tools.add("flatpak")
    box.sh.fail["repo"], box.sh.fail["flatpak"] = [failed(CONFLICT)], [failed(NOT_FOUND)]
    report = box.run()
    assert report["repairs"] == [
        {"case": "MIRROR-STALE-404", "repair": "wait-for-mirror", "attempted": True},
        {"case": "PACMAN-REPLACEMENT-CONFLICT", "repair": None, "attempted": False},  # asked last: nothing on record
    ]
    assert box.sh.count("flatpak") == 2 and box.sh.count("repo") == 1 and cases_of(report) == [("repo", "PACMAN-REPLACEMENT-CONFLICT")]


def test_a_store_that_failed_for_good_is_not_run_again_after_another_is_repaired(box) -> None:
    box.sh.fail["repo"], box.sh.fail["aur"] = [failed(NOT_FOUND)], [failed("-> error making: foo - exit status 4")]
    report = box.run()
    assert box.sh.count("repo") == 2 and box.sh.count("aur") == 1
    assert cases_of(report) == [("aur", "AUR-BUILD-LAYER-FAILURE")] and report["exit_code"] == exits.RECOVERY_PENDING


def test_a_halt_after_changes_still_describes_what_was_left(box) -> None:
    box.sh.fail["aur"] = [failed(UNSEEN)]
    report = box.run()
    assert report["updated"] == {"repo": 1} and report["looks"] == 2, "one look before, one after the halt"


def test_the_closing_look_decides_nothing(box) -> None:
    box.config(conditions=[{"name": "home", "argv": ["at-home"], "exit": 40}])
    box.sh.fail["aur"] = [failed(UNSEEN)]
    box.sh.fail["at-home"] = [ok(), failed("left home")]
    report = box.run()
    assert report["exit_code"] == exits.RECOVERY_PENDING, "the update's failure is the cause, not the later look"
    assert [probe for probe, _ in cases_of(report)] == ["aur"]


def test_a_dry_run_changes_nothing_and_takes_nothing(box) -> None:
    with (box.root / "auto.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        report = box.run(dry_run=True)
    assert report["state"] == "DRY_RUN" and box.sh.ran == []
    assert not (box.root / "keep").exists() and not (box.root / "last-auto.json").exists()
    assert not any(call[0] == "notify-send" for call in box.sh.calls)


def test_a_listing_that_cannot_be_read_is_not_a_change(box) -> None:
    box.sh.fail["pacman -Q"] = [ok("a 1\n"), ok("a 1\n"), failed("error: could not open database")]
    report = box.run()
    assert report["updated"] == {}


def test_a_removal_is_a_change(box) -> None:
    box.sh.packages[:] = ["a 2", "gone 1"]
    plain = box.sh.logged

    def upgrade_removes(argv, log, timeout=0):
        result = plain(argv, log)
        if argv == UPGRADE:
            box.sh.packages[:] = ["a 2"]
        return result

    box.patch.setattr(box.sh, "logged", upgrade_removes)
    assert box.run()["updated"] == {"repo": 1}


# ── AUR ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [False, None, "true", "false", 1, "yes"])
def test_unattended_aur_needs_a_real_true(box, value) -> None:
    box.config(unattended_aur=value)
    report = box.run()
    assert report["state"] == "IDLE" and "aur" in report["held"] and box.sh.count("aur") == 0


def test_aur_packages_changed_in_the_last_days_wait(box) -> None:
    now = time.time()
    box.sh.aur["results"] = [{"Name": "old", "LastModified": now - 10 * 86400}, {"Name": "new", "LastModified": now - 3600}]
    report = box.run()
    yay = next(argv for argv in box.sh.ran if argv[0] == "yay")
    assert yay[-2:] == ["--ignore", "new"] and report["held"] == {"aur_too_fresh": ["new"]}
    box.config(aur_min_age_days=30)
    assert box.run()["held"] == {"aur_too_fresh": ["new", "old"]}


@pytest.mark.parametrize("answer", [OSError("Network is unreachable"), {"type": "error", "error": "Too many", "results": []}, {"results": []}])
def test_when_aur_does_not_answer_properly_nothing_from_it_is_built(box, answer) -> None:
    box.sh.aur = answer
    report = box.run()
    assert box.sh.count("aur") == 0 and box.sh.count("repo") == 1
    assert cases_of(report) == [("aur", "AUR-UNREACHABLE")] and report["exit_code"] == exits.RECOVERY_PENDING
    assert len(report["repairs"]) == 3 and report["updated"] == {"repo": 1}


def test_aur_is_not_asked_when_the_foreign_listing_fails(box) -> None:
    box.sh.fail["pacman -Qqm"] = [failed("error: database", 2)]
    with pytest.raises(OSError, match="pacman -Qqm failed"):
        stores.aur_too_fresh(box.sh, 4)


# ── the record made before the first change ────────────────────────────────


def test_no_record_no_update(box) -> None:
    box.sh.fail["pacman -Qqe"] = [failed("error: failed to initialize alpm library", 1)]
    report = box.run()
    assert report["state"] == "BACKUP_BLOCKED" and report["exit_code"] == 50
    assert box.sh.ran == [] and cases_of(report) == [("keep", "KEEP-UNWRITABLE")]


def test_the_record_is_written_once_before_the_first_change(box) -> None:
    box.sh.fail["repo"] = [failed(NOT_FOUND)]
    report = box.run()
    assert sum(argv[0] == "tar" for argv in box.sh.ran) == 1
    assert (box.root / "keep" / report["run_id"] / "packages.txt").read_text() == "a 1\n"


def test_keep_prunes_only_its_own_older_records_and_never_the_new_one(box) -> None:
    keep = box.root / "keep"
    future = [f"auto-2099010{day}T120000+0300" for day in range(1, 7)]
    for name in [*future, "auto-backup", "photos"]:
        (keep / name).mkdir(parents=True)
    report = box.run()
    assert sorted(path.name for path in keep.iterdir()) == sorted([*future[3:], report["run_id"], "auto-backup", "photos"])


def test_a_configured_keep_must_be_another_disk_that_is_there(box, tmp_path, monkeypatch) -> None:
    system, other = tmp_path / "system-disk", tmp_path / "other-disk"
    system.mkdir()
    other.mkdir()
    monkeypatch.setattr(watch, "device", lambda path: 1 if path == Path("/") or system in (path, *path.parents) else 2)

    def verdict(config):
        return watch.probe_keep(watch.Scene(box.sh, config, tmp_path / "auto.json", auto.World(box.sh, config, [], "auto-1", tmp_path / "runs" / "auto-1").keep_root(), None)).verdicts[0]

    assert verdict({}).status == "pass"
    assert verdict({"keep": str(other / "keep")}).status == "pass", "another disk, folder not made yet"
    on_system_disk = str(system / "keep")
    assert "system disk" in verdict({"keep": on_system_disk}).detail, "an unmounted disk leaves its mount point on the system disk"
    assert verdict({"keep": on_system_disk, "keep_on_system_disk": True}).status == "pass"
    plain_file = other / "file"
    plain_file.write_text("")
    assert (verdict({"keep": str(plain_file)}).status, verdict({"keep": str(plain_file)}).code) == ("blocked", 50)


def test_a_missing_keep_folder_is_made_private_instead_of_failing_the_run(box, tmp_path) -> None:
    keep = tmp_path / "disk" / "arch-keep"
    box.config(keep=str(keep), keep_on_system_disk=True)
    report = box.run()
    assert report["exit_code"] in (0, 10), report["trouble"]
    assert (keep / report["run_id"] / "packages.txt").is_file()
    assert keep.stat().st_mode & 0o777 == 0o700


# ── the run as a whole ─────────────────────────────────────────────────────


def test_any_crash_ends_in_a_report_and_a_notification(box) -> None:
    box.patch.setattr(auto.World, "_apply", lambda self: 1 / 0)
    report = box.run()
    assert report["state"] == "TOOL_FAILURE" and report["exit_code"] == 70 and report["crash"].startswith("ZeroDivisionError")
    assert (box.root / "last-auto.json").is_file()
    told = [call for call in box.sh.calls if call[0] == "notify-send"]
    assert told[-1][1] == "--urgency=critical" and "ZeroDivisionError" in told[-1][3]


@pytest.mark.parametrize("config", ['{"conditions": 5}', "{not json", '{"conditions": [{"name": "x"}]}'])
def test_a_broken_config_never_reaches_a_package_manager(box, config) -> None:
    (box.root / "auto.json").write_text(config)
    report = box.run()
    assert report["out"] == 0 and box.sh.ran == [] and report["exit_code"] in (exits.TOOL_FAILURE, exits.BLOCKED)


def test_a_bad_cve_finding_does_not_lose_the_report(box) -> None:
    box.patch.setattr(auto, "scan_cves", lambda: {"available": True, "findings": ["not a dict"]})
    assert box.run()["cve"] == {"known": True, "findings": 1, "by_severity": {"Unknown": 1}}


def test_a_second_run_under_the_lock_is_skipped_and_says_so(box) -> None:
    with (box.root / "auto.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        report = box.run()
    assert report["state"] == "SKIPPED" and report["exit_code"] == 80 and not (box.root / "runs").exists()
    assert [call[2] for call in box.sh.calls if call[0] == "notify-send"] == ["arch-update auto: SKIPPED"]


def test_a_click_on_the_notice_opens_a_terminal_with_the_fix(box) -> None:
    box.sh.tools |= {"systemd-run", "kitty"}
    box.sh.fail["repo"] = [failed(CONFLICT)]
    box.run()
    asked = box.sh.calls[-1]
    assert asked[:4] == ["systemd-run", "--user", "--collect", "--quiet"], "the wait for the click is its own unit"
    fix = census.find(census.load(box.root), "PACMAN-REPLACEMENT-CONFLICT").fix
    assert f"--setenv=FIX={fix}" in asked and "--setenv=OPEN=kitty" in asked
    notice = asked[asked.index("notify-send") :]
    assert notice[:4] == ["notify-send", "--urgency=critical", "--wait", f"--action=default={fix}"]
    assert notice[4] == "arch-update auto: RECOVERY_PENDING"


def test_a_clean_week_with_aur_held_offers_the_aur_update_on_click(box) -> None:
    box.sh.tools |= {"systemd-run", "kitty"}
    box.config(unattended_aur=False)
    box.run()
    asked = box.sh.calls[-1]
    assert "--setenv=FIX=yay -Sua" in asked and "--urgency=normal" in asked


@pytest.mark.parametrize("code", [129, 130, 143])
def test_a_run_ended_by_any_stop_signal_is_announced_quietly_as_interrupted(box, code) -> None:
    report = {"exit_code": code, "state": exits.NAMES[code], "crash": None, "trouble": [], "updated": {}, "held": {}}
    auto.announce(box.sh, report)
    assert box.sh.calls[-1][:3] == ["notify-send", "--urgency=normal", "arch-update auto: INTERRUPTED"]


def test_a_failed_run_is_still_announced_as_critical(box) -> None:
    auto.announce(box.sh, {"exit_code": exits.TOOL_FAILURE, "state": "TOOL_FAILURE", "crash": None, "trouble": [], "updated": {}, "held": {}})
    assert box.sh.calls[-1][:2] == ["notify-send", "--urgency=critical"]


def test_the_held_aur_reason_tells_the_command_and_promises_no_click(box) -> None:
    box.config(unattended_aur=False)
    report = box.run()
    assert "click" not in report["held"]["aur"] and "yay -Sua" in report["held"]["aur"]
    assert "click" not in auto.status()


def test_the_click_script_runs_the_fix_in_the_terminal_only_when_clicked(tmp_path) -> None:
    env = {"PATH": "/usr/bin:/bin", "FIX": f"echo fixed > {tmp_path}/done", "OPEN": "env"}
    for answer, ran in (("dismissed", False), ("default", True)):
        subprocess.run(["sh", "-c", auto.CLICK_SCRIPT, "sh", "echo", answer], env=env, stdin=subprocess.DEVNULL, check=False)
        assert (tmp_path / "done").exists() is ran


def test_without_a_terminal_the_notice_is_still_sent(box) -> None:
    box.sh.tools.add("systemd-run")
    box.sh.fail["repo"] = [failed(CONFLICT)]
    box.run()
    assert box.sh.calls[-1][:2] == ["notify-send", "--urgency=critical"]


def test_a_stop_request_between_two_stores_starts_no_other(box) -> None:
    box.sh.tools.add("flatpak")
    world = auto.World(box.sh, {"unattended_aur": True}, census.load(box.root), "auto-1", box.root)
    world.kept = True
    plain = box.sh.capture

    def capture(argv, timeout=30):  # the handler's two assignments, landing as the first store ends
        if box.sh.ran and argv[:2] == ["pacman", "-Q"]:
            world.stop, world.signum = True, signal.SIGTERM
        return plain(argv, timeout)

    box.patch.setattr(box.sh, "capture", capture)
    with pytest.raises(exits.Stopped) as stopped:
        world._apply()
    assert stopped.value.signum == signal.SIGTERM
    assert box.sh.ran == [UPGRADE] and world.done == {"repo"} and not world.failed
    assert world._census() == 0, "no repair is started after a stop either"


def test_two_runs_in_one_second_get_separate_evidence(box) -> None:
    frozen = auto.datetime.now().astimezone()
    box.patch.setattr(auto, "datetime", type("Clock", (auto.datetime,), {"now": classmethod(lambda cls, tz=None: frozen)}))
    first, second = box.run(dry_run=True), box.run(dry_run=True)
    assert second["run_id"] == first["run_id"] + "-1" and len(list((box.root / "runs").iterdir())) == 2


def test_status_lists_what_hangs_with_the_command_that_fixes_it(box) -> None:
    assert auto.status() == "no unattended run on record yet\n"
    box.sh.fail["repo"] = [failed(CONFLICT)]
    box.run()
    text = auto.status()
    assert text.startswith("RECOVERY_PENDING") and "repo: PACMAN-REPLACEMENT-CONFLICT" in text and 'fix: grep -h "in conflict"' in text
    (box.root / "last-auto.json").write_text("{cut short")
    assert auto.status() == "no unattended run on record yet\n" and scheduler.auto_status()["last_run"] is None


def test_init_writes_the_starter_config_once(box) -> None:
    (box.root / "auto.json").unlink()
    assert auto.init_config().startswith("wrote ") and json.loads((box.root / "auto.json").read_text())["unattended_aur"] is False
    (box.root / "auto.json").write_text('{"mine": 1}')
    assert "already exists" in auto.init_config() and json.loads((box.root / "auto.json").read_text()) == {"mine": 1}


def test_the_command_exits_with_the_state_of_the_run(box, capsys) -> None:
    from arch_updater import main

    box.patch.setattr(main, "run_auto", lambda dry_run=False: box.run(dry_run=dry_run))
    box.sh.fail["repo"] = [failed(CONFLICT)]
    assert main.main(["auto"]) == exits.RECOVERY_PENDING and '"state": "RECOVERY_PENDING"' in capsys.readouterr().out
    box.patch.setattr(main, "install_weekly_auto_timer", lambda: {"installed": False})
    assert main.main(["schedule", "--install-auto"]) == 1


# ── root, the unit, the shell ──────────────────────────────────────────────


def test_default_sudoers_rules_are_exact_commands_built_from_what_is_run(box) -> None:
    box.sh.tools |= {"true", "paccache", "mkinitcpio", "dkms"}
    text = auto.sudoers_rules("u", sh=box.sh)
    assert "/etc/sudoers.d/zzz-arch-update-auto" in text, "the file must sort after the password rule, or that rule wins"
    rules = [line for line in text.splitlines() if not line.startswith("#")]
    assert rules == [
        "u ALL=(root) NOPASSWD: /usr/bin/true",
        "u ALL=(root) NOPASSWD: /usr/bin/pacman -Syu --noconfirm",
        "u ALL=(root) NOPASSWD: /usr/bin/paccache -rk2",
        "u ALL=(root) NOPASSWD: /usr/bin/mkinitcpio -P",
        "u ALL=(root) NOPASSWD: /usr/bin/dkms autoinstall",
    ]
    assert repairs.sudo(stores.STORES[0].update) == UPGRADE
    with_aur = auto.sudoers_rules("u", aur=True, sh=box.sh)
    assert with_aur.count("*") == 4 and "/usr/bin/pacman -U --noconfirm --config /etc/pacman.conf -- " in with_aur


def test_the_weekly_unit_runs_this_launcher_and_spares_the_package_manager(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "run_capture", lambda *args, **kwargs: ok())
    monkeypatch.setattr(scheduler.shutil, "which", lambda tool: "/opt/deck/bin/arch-update")
    assert scheduler.install_weekly_auto_timer(unit_dir=tmp_path)["installed"] is True
    service = (tmp_path / "arch-update-auto.service").read_text()
    assert "ExecStart=/opt/deck/bin/arch-update auto --scheduled\n" in service
    assert "KillMode=mixed\n" in service and "SuccessExitStatus=10 80\n" in service
    timer = (tmp_path / "arch-update-auto.timer").read_text()
    assert "OnCalendar=Sat 12:00\n" in timer and "Persistent=true\n" in timer


def test_a_logged_command_is_on_disk_before_it_runs_and_does_not_read_stdin(tmp_path) -> None:
    log = tmp_path / "run.log"
    result = Shell().logged(["sh", "-c", f"grep -c 'sh -c grep' {log}; read line; echo got:$line; exit 3"], log)
    assert result.returncode == 3 and result.output.splitlines()[:2] == ["1", "got:"]
    assert log.read_text().endswith("[exit 3]\n")
    assert Shell().logged(["no-such-program-here"], log).returncode == 127


def test_a_command_past_its_time_is_stopped_with_everything_it_started(tmp_path) -> None:
    log = tmp_path / "run.log"
    started = time.monotonic()
    result = Shell().logged(["sh", "-c", "sleep 30 & sleep 30"], log, timeout=1)
    assert result.returncode == 124 and time.monotonic() - started < 10


def test_captured_commands_never_read_the_callers_stdin() -> None:
    assert Shell().capture(["sh", "-c", "read line; echo got:$line"]).output == "got:\n"


def test_a_non_https_api_is_refused() -> None:
    with pytest.raises(ValueError, match="non-https"):
        Shell().post("http://aur.archlinux.org/rpc", [])


def test_a_package_transaction_is_never_timed_out(tmp_path) -> None:
    log = tmp_path / "run.log"
    fake = tmp_path / "pacman"
    fake.write_text("#!/bin/sh\nsleep 2\nexit 0\n")
    fake.chmod(0o755)
    result = Shell().logged([str(fake), "-Syu"], log, timeout=1)
    assert result.returncode == 0, "a pacman run must be left to finish"


def test_sudoers_commands_come_from_root_owned_system_directories_only(tmp_path) -> None:
    fixed = tmp_path / "bin"
    fixed.mkdir()
    good = fixed / "good"
    loose = fixed / "loose"
    for program in (good, loose):
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
    loose.chmod(0o775)
    sh = Shell()
    # The files belong to the test user, not root, so nothing here qualifies...
    path, reason = sh.root_binary("good", dirs=(str(fixed),))
    assert path is None and "not owned by root" in reason
    assert sh.root_binary("absent", dirs=(str(fixed),))[0] is None
    # ...and with ownership faked as root, only the mode separates the two.
    real_stat = Path.stat
    owned_by_root = lambda self, **kw: type("S", (), {"st_uid": 0, "st_mode": real_stat(self).st_mode})()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "stat", owned_by_root)
        assert sh.root_binary("good", dirs=(str(fixed),)) == (str(good), "")
        path, reason = sh.root_binary("loose", dirs=(str(fixed),))
        assert path is None and "writable" in reason


def test_a_command_that_cannot_be_trusted_gets_no_sudoers_rule_and_says_so(box) -> None:
    box.sh.tools |= {"true", "paccache"}
    plain = box.sh.root_binary
    box.patch.setattr(box.sh, "root_binary", lambda tool, dirs=(): (None, "/usr/bin/paccache is writable by its group or by others") if tool == "paccache" else plain(tool))
    text = auto.sudoers_rules("u", sh=box.sh)
    assert "# skipped paccache: /usr/bin/paccache is writable" in text
    assert not any("paccache" in line for line in text.splitlines() if not line.startswith("#"))


def test_a_dry_run_says_where_it_wrote_and_that_no_root_is_expected_before_the_sudoers_rule(box, capsys) -> None:
    from arch_updater import main

    box.patch.setattr(main, "run_auto", lambda dry_run=False: box.run(dry_run=dry_run))
    box.sh.fail["sudo -n"] = [failed("sudo: a password is required")]
    main.main(["auto", "--dry-run"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["wrote"].startswith(str(box.root)) and report["wrote"] in captured.err
    assert "NO-ROOT is expected until the sudoers rule is installed" in captured.err
    main.main(["auto", "--dry-run"])
    assert "NO-ROOT" not in capsys.readouterr().err, "the hint only shows when the rule really is missing"


def test_sudoers_rules_name_the_account_that_runs_not_the_environment(monkeypatch, capsys) -> None:
    from arch_updater import main

    monkeypatch.setenv("USER", "spoofed")
    monkeypatch.setenv("LOGNAME", "spoofed")
    assert main.main(["auto", "--sudoers"]) == 0
    out = capsys.readouterr().out
    import os
    import pwd

    real = pwd.getpwuid(os.getuid()).pw_name
    assert out.strip() and "spoofed" not in out
    assert all(line.startswith(f"{real} ALL=(root) NOPASSWD:") for line in out.splitlines() if line and not line.startswith("#"))


# ── what a failure says: its cause, not the `[exit N]` the log adds ─────────


def really_failed(tmp_path, text="error: unresolvable package conflicts detected"):
    """A failed command as Shell.logged really reports it: the output, then the `[exit N]` line it appends."""
    result = Shell().logged(["sh", "-c", f"echo '{text}'; exit 1"], tmp_path / "log")
    assert result.output.rstrip().endswith("[exit 1]"), "the shape under test"
    return result


def test_a_failure_names_its_cause_on_every_surface_not_the_exit_line(box, tmp_path) -> None:
    box.sh.fail["repo"] = [really_failed(tmp_path)]
    report = box.run()
    why = "error: unresolvable package conflicts detected"
    assert why in report["trouble"][0]["detail"], "the evidence keeps the cause"
    assert f"— {why}" in auto.status() and "[exit" not in auto.status()
    told = [call for call in box.sh.calls if call[0] == "notify-send"]
    assert why in told[-1][3] and "[exit" not in told[-1][3]


@pytest.mark.parametrize(
    ("detail", "line"),
    [
        ("error: x\n\n[exit 1]\n", "error: x"),
        ("a\nerror: y\n  \n[exit -15]\n\n", "error: y"),
        ("only this", "only this"),
        ("[exit 1]\n", ""),
        ("", ""),
    ],
)
def test_cause_is_the_last_line_with_words_in_it(detail, line) -> None:
    assert cause(detail) == line



# ── SIGTERM while a store runs ends the run as interrupted ──────────────────


def test_sigterm_while_a_store_runs_leaves_it_alone_and_ends_the_run_as_interrupted(box) -> None:
    import os
    import signal

    box.sh.tools.add("flatpak")
    plain = box.sh.logged
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: (os.kill(os.getpid(), signal.SIGTERM) if argv == UPGRADE else None, plain(argv, log))[1])
    before = signal.getsignal(signal.SIGTERM)
    report = box.run()
    assert report["state"] == "INTERRUPTED" and report["exit_code"] == 128 + signal.SIGTERM == 143 and report["signal"] == signal.SIGTERM
    assert box.sh.ran[-1] == UPGRADE and box.sh.count("flatpak") == 0, "the running command ended on its own and nothing else was started"
    assert report["child_stopped"] is False, "it was not sent anything and it ended with 0: it finished"
    assert report["trouble"][0]["detail"] == "stopped by SIGTERM"
    assert signal.getsignal(signal.SIGTERM) == before and json.loads((box.root / "last-auto.json").read_text())["state"] == "INTERRUPTED"
