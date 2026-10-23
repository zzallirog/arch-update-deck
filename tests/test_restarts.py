"""After an update: what still runs replaced code, read from a /proc built in a temporary folder."""

from __future__ import annotations

import json
import os

import pytest

from arch_updater import auto, day, restarts
from test_auto import box  # noqa: F401 - the scripted machine, as a fixture

LIBSSL = "7f00-7f10 r-xp 00000000 00:1f 123 /usr/lib/libssl.so.3 (deleted)\n"
SERVICE = "0::/system.slice/sshd.service\n"
USER_SERVICE = "0::/user.slice/user-1000.slice/user@1000.service/session.slice/pipewire.service\n"
SCOPE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox-1234.scope\n"


def process(proc, pid, maps, cgroup=SERVICE, comm="sshd"):
    folder = proc / str(pid)
    folder.mkdir(parents=True)
    (folder / "maps").write_text(maps)
    (folder / "cgroup").write_text(cgroup)
    (folder / "comm").write_text(comm + "\n")
    return folder


@pytest.mark.parametrize(
    ("line", "found"),
    [
        (LIBSSL, ["/usr/lib/libssl.so.3"]),
        ("7f00-7f10 r--p 00000000 00:1f 9 /usr/lib/libc.so.6 (deleted)\n", ["/usr/lib/libc.so.6"]),  # a library, whatever its rights
        ("5600-5610 r-xp 00000000 00:1f 9 /usr/bin/sshd (deleted)\n", ["/usr/bin/sshd"]),  # the program itself, mapped to run
        ("7f00-7f10 r-xp 00000000 00:1f 9 /opt/app/lib/libx.so (deleted)\n", ["/opt/app/lib/libx.so"]),
        ("7f00-7f10 r-xp 00000000 00:1f 123 /usr/lib/libssl.so.3\n", []),  # still there: not replaced
        ("7f00-7f10 rw-p 00000000 00:1f 9 /usr/share/icons/cache (deleted)\n", []),  # data, not code
        ("7f00-7f10 r-xp 00000000 00:1f 9 /tmp/.mount_app/libx.so (deleted)\n", []),  # not a packaged tree
        ("7f00-7f10 r-xp 00000000 00:01 9 /memfd:jit (deleted)\n", []),
        ("7f00-7f10 r-xp 00000000 00:1f 9 /home/u/lib/libx.so (deleted)\n", []),
        ("7f00-7f10 r-xp 00000000 00:00 0 \n", []),  # anonymous
    ],
)
def test_only_packaged_code_that_was_replaced_counts(line, found) -> None:
    assert restarts.stale(line) == found


@pytest.mark.parametrize(
    ("cgroup", "unit"),
    [
        (SERVICE, ("system", "sshd.service")),
        ("0::/system.slice/system-getty.slice/getty@tty1.service\n", ("system", "getty@tty1.service")),
        (USER_SERVICE, ("user", "pipewire.service")),
        (SCOPE, ("", "")),
        ("0::/user.slice/user-1000.slice/user@1000.service/init.scope\n", ("", "")),
        ("0::/user.slice/user-1000.slice/session-2.scope\n", ("", "")),
        ("12:pids:/\n1:name=systemd:/system.slice/cups.service\n0::/system.slice/cups.service\n", ("system", "cups.service")),
        ("", ("", "")),
    ],
)
def test_a_process_is_placed_in_its_service_by_its_cgroup(cgroup, unit) -> None:
    assert restarts.unit_of(cgroup) == unit


def test_services_and_programs_holding_replaced_code_are_named_for_a_person(tmp_path) -> None:
    proc = tmp_path / "proc"
    process(proc, 400, LIBSSL)
    process(proc, 401, LIBSSL)  # a second process of the same service: one name
    process(proc, 500, LIBSSL, USER_SERVICE, "pipewire")
    process(proc, 600, LIBSSL, SCOPE, "firefox")
    process(proc, 700, "7f00-7f10 r-xp 00000000 00:1f 123 /usr/lib/libssl.so.3\n", "0::/system.slice/fresh.service\n", "fresh")
    (proc / "self").mkdir()  # not a process
    found = restarts.scan(proc)
    assert (found.system, found.user, found.programs) == (["sshd.service"], ["pipewire.service"], ["firefox"])
    assert not found.reboot and found.complete and found.unread == 0
    assert restarts.commands(found.as_dict()) == ["sudo systemctl restart sshd.service", "systemctl --user restart pipewire.service"]


@pytest.mark.parametrize(("pid", "comm"), [(1, "systemd"), (1, "init"), (900, "systemd"), (901, "dbus-broker"), (902, "dbus-daemon"), (903, "dbus-broker-lau")])
def test_the_manager_or_the_bus_on_replaced_code_needs_a_reboot(tmp_path, pid, comm) -> None:
    proc = tmp_path / "proc"
    process(proc, pid, LIBSSL, "0::/init.scope\n", comm)
    found = restarts.scan(proc)
    assert found.reboot and found.why == [f"{comm} (pid {pid}) uses the replaced libssl.so.3"]
    assert found.system == found.user == found.programs == [], "a reboot covers it: it is not offered as a restart"


def test_a_manager_on_current_code_needs_nothing(tmp_path) -> None:
    proc = tmp_path / "proc"
    process(proc, 1, "7f00-7f10 r-xp 00000000 00:1f 123 /usr/lib/libc.so.6\n", "0::/init.scope\n", "systemd")
    assert restarts.scan(proc) == restarts.Restarts()


@pytest.mark.skipif(os.geteuid() == 0, reason="uid 0 reads every file")
def test_a_process_that_cannot_be_read_makes_the_scan_incomplete_and_is_counted(tmp_path) -> None:
    proc = tmp_path / "proc"
    process(proc, 1, LIBSSL, "0::/init.scope\n", "systemd").joinpath("maps").chmod(0)
    process(proc, 400, LIBSSL)
    found = restarts.scan(proc)
    assert found.unread == 1 and not found.complete and not found.reboot, "what was not read is not guessed either way"
    assert found.system == ["sshd.service"]


def test_a_process_that_ended_while_being_read_is_skipped(tmp_path) -> None:
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)  # its files are gone: it ended between the listing and the read
    assert restarts.scan(proc) == restarts.Restarts()


# ── in the run: the report, the notice, `auto --status`, the screen ──────────


def test_a_run_that_replaced_packages_reports_the_scan(box, tmp_path) -> None:  # noqa: F811
    process(restarts.PROC, 400, LIBSSL)
    report = box.run()
    assert report["updated"] == {"repo": 1}
    assert report["restart"] == restarts.Restarts(system=["sshd.service"]).as_dict(), "only the test's /proc was read"
    assert report["reboot_required"] is False and report["exit_code"] == 0, "a restart changes neither the exit nor the kernel's reboot"
    assert "restart: sshd.service" in box.sh.calls[-1][-1]
    assert json.loads((tmp_path / "last-auto.json").read_text())["restart"]["system"] == ["sshd.service"]


def test_a_run_that_changed_nothing_does_not_look(box) -> None:  # noqa: F811
    process(restarts.PROC, 400, LIBSSL)
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: type("R", (), {"returncode": 0, "output": ""})())
    report = box.run()
    assert report["updated"] == {} and report["restart"] is None


def test_a_dry_run_does_not_look(box) -> None:  # noqa: F811
    process(restarts.PROC, 400, LIBSSL)
    assert box.run(dry_run=True)["restart"] is None


def test_status_and_the_screen_say_what_to_restart_and_why_a_reboot(box) -> None:  # noqa: F811
    process(restarts.PROC, 400, LIBSSL)
    process(restarts.PROC, 600, LIBSSL, SCOPE, "firefox")
    process(restarts.PROC, 1, LIBSSL, "0::/init.scope\n", "systemd")
    report = box.run()
    status = auto.status()
    assert "restart: sudo systemctl restart sshd.service" in status
    assert "still running replaced files: firefox" in status
    assert "reboot needed: systemd (pid 1) uses the replaced libssl.so.3" in status
    headline, lines = day.summary(report)
    text = "\n".join(lines)
    assert "restart by hand: " in text and "sudo systemctl restart sshd.service" in text and "firefox" in text
    assert "reboot needed: systemd (pid 1)" in text
    assert "REBOOT" not in headline, "the headline's REBOOT NEEDED stays the kernel's, as exit 10 is"
    assert "reboot needed: systemd" in box.sh.calls[-1][-1]


def test_the_screen_and_status_say_when_system_services_were_not_looked_into(box) -> None:  # noqa: F811
    found = restarts.Restarts(unread=7, complete=False).as_dict()
    text = "\n".join(day.summary({"updated": {"repo": 1}, "exit_code": 0, "restart": found})[1])
    assert "system services not looked into: 7 processes" in text
    box.run()
    report = json.loads((auto.STATE_ROOT / "last-auto.json").read_text())
    report["restart"] = found
    (auto.STATE_ROOT / "last-auto.json").write_text(json.dumps(report))
    assert "not looked into: 7 processes of other accounts" in auto.status()
