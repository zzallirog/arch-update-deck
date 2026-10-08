"""The unattended run waits when someone is bothered, and runs under a ceiling: on fake /proc and /sys trees."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from arch_updater import day, exits, quiet


def proc_with(tmp_path: Path, comms: dict[str, str] | None = None, pressure: dict[str, float] | None = None, cgroup: str = "") -> Path:
    proc = Path(tempfile.mkdtemp(dir=tmp_path, prefix="proc-"))
    for pid, comm in (comms or {}).items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "comm").write_text(comm + "\n")
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    for kind, avg60 in (pressure or {}).items():
        (proc / "pressure" / kind).write_text(f"some avg10=0.00 avg60={avg60:.2f} avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    if cgroup:
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cgroup").write_text(f"0::{cgroup}\n")
    return proc


def sys_with(tmp_path: Path, mains_online: str | None = None, smt_pairs: int = 0) -> Path:
    root = Path(tempfile.mkdtemp(dir=tmp_path, prefix="sys-"))
    supply = root / "class" / "power_supply"
    supply.mkdir(parents=True)
    if mains_online is not None:
        (supply / "ACAD").mkdir()
        (supply / "ACAD" / "type").write_text("Mains\n")
        (supply / "ACAD" / "online").write_text(mains_online + "\n")
    (supply / "BAT0").mkdir()
    (supply / "BAT0" / "type").write_text("Battery\n")
    for cpu in range(2 * smt_pairs):  # cpu i and i+n share a core, as on most x86 machines
        topo = root / "devices/system/cpu" / f"cpu{cpu}" / "topology"
        topo.mkdir(parents=True)
        topo.joinpath("thread_siblings_list").write_text(f"{cpu % smt_pairs},{cpu % smt_pairs + smt_pairs}\n")
    return root


def test_a_game_makes_the_run_wait(tmp_path) -> None:
    assert quiet.gate({}, sys_with(tmp_path, "1"), proc_with(tmp_path, {"1": "systemd", "42": "steam_app_2073850"})) == "a game is running (steam_app_2073850)"
    assert quiet.gate({}, sys_with(tmp_path, "1"), proc_with(tmp_path, {"1": "systemd", "7": "steam"})) is None


def test_pressure_over_its_limit_makes_the_run_wait(tmp_path) -> None:
    busy = proc_with(tmp_path, pressure={"cpu": 55.0, "memory": 0.0})
    assert "cpu pressure 55%" in quiet.gate({}, sys_with(tmp_path, "1"), busy)
    assert quiet.gate({"quiet_pressure": {"cpu": 80}}, sys_with(tmp_path, "1"), busy) is None


def test_battery_makes_the_run_wait_and_a_desktop_is_never_on_battery(tmp_path) -> None:
    assert quiet.gate({}, sys_with(tmp_path, "0"), proc_with(tmp_path)) == "on battery"
    assert quiet.gate({}, sys_with(tmp_path, None), proc_with(tmp_path)) is None


def test_what_cannot_be_read_lets_the_run_go(tmp_path) -> None:
    assert quiet.gate({}, tmp_path / "no-sys", tmp_path / "no-proc") is None
    assert quiet.gate({"quiet": False}, sys_with(tmp_path, "0"), proc_with(tmp_path)) is None


def test_the_ceiling_takes_one_thread_of_a_quarter_of_the_cores(tmp_path) -> None:
    root = sys_with(tmp_path, smt_pairs=8)  # 8 cores, 16 threads
    picked = quiet.cores(set(range(16)), root)
    assert picked == [0, 1] and len({cpu % 8 for cpu in picked}) == len(picked)
    assert quiet.cores({3}, root) == [3]  # one allowed thread: still one


def test_the_ceiling_goes_on_the_service_only(tmp_path) -> None:
    calls = []
    run = lambda argv: calls.append(argv) or 0  # noqa: E731
    root = sys_with(tmp_path, smt_pairs=8)
    assert quiet.calm({}, run, proc_with(tmp_path, cgroup="/user.slice/app.slice/app-kitty.scope"), root) == []
    assert calls == []  # a run by hand: a person waits for it
    done = quiet.calm({}, run, proc_with(tmp_path, cgroup="/user.slice/app.slice/arch-update-auto.service"), root)
    assert calls[0][:5] == ["systemctl", "--user", "set-property", "--runtime", "arch-update-auto.service"]
    assert "CPUQuota=" in " ".join(calls[0]) and any(item.startswith("AllowedCPUs=") for item in calls[0])
    assert done and done[0].startswith("arch-update-auto.service")


def test_a_waiting_run_is_skipped_not_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(quiet, "PROC", proc_with(tmp_path, {"9": "gamescope"}))
    ran = []
    monkeypatch.setattr(day.auto, "run_auto", lambda **kw: ran.append(1) or {"exit_code": 0})
    assert day.scheduled() == exits.SKIPPED and not ran


def test_no_test_can_reach_the_real_sudo_or_change_a_real_unit() -> None:
    import subprocess

    assert subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 97
    assert subprocess.run(["systemctl", "--user", "set-property", "--runtime", "x.service", "CPUWeight=1"], capture_output=True).returncode == 97


def test_the_ceiling_is_counted_from_the_machine_not_from_what_an_earlier_one_left(tmp_path) -> None:
    root = sys_with(tmp_path, smt_pairs=8)
    (root / "devices/system/cpu/online").write_text("0-15\n")
    first = quiet.cores(sys_root=root)
    assert quiet.cores(sys_root=root) == first == [0, 1]  # no ratchet: the same every run


def test_the_ceiling_is_lifted_first_and_quiet_false_only_lifts_it(tmp_path) -> None:
    calls = []
    run = lambda argv: calls.append(argv) or 0  # noqa: E731
    svc = proc_with(tmp_path, cgroup="/user.slice/arch-update-auto.service")
    root = sys_with(tmp_path, smt_pairs=8)
    quiet.calm({}, run, svc, root)
    assert calls[0][-4:] == ["AllowedCPUs=", "CPUQuota=", "CPUWeight=", "IOWeight="]  # released before it is counted
    calls.clear()
    assert quiet.calm({"quiet": False}, run, svc, root) == [] and len(calls) == 1 and calls[0][-1] == "IOWeight="
    assert quiet.release(run, svc) and quiet.release(run, proc_with(tmp_path)) is False


def test_waiting_is_bounded(tmp_path) -> None:
    now = 1_000_000.0
    assert quiet.waited("on battery", tmp_path, now) == "on battery"
    assert quiet.waited("on battery", tmp_path, now + 2 * 86400) == "on battery"
    assert quiet.waited("on battery", tmp_path, now + quiet.MAX_WAIT_DAYS * 86400) is None  # goes anyway
    assert quiet.waited("on battery", tmp_path, now + 4 * 86400) == "on battery"  # a new wait starts
    assert quiet.waited(None, tmp_path, now) is None and not (tmp_path / quiet.WAITING).exists()


@pytest.mark.parametrize("config", [{"quiet_share": 0}, {"quiet_share": "x"}, {"quiet_pressure": [1, 2]}, {"quiet_pressure": {"cpu": "high"}}])
def test_a_setting_that_makes_no_sense_lets_the_run_go(tmp_path, config) -> None:
    busy = proc_with(tmp_path, pressure={"cpu": 99.0})
    assert quiet.gate(config, sys_with(tmp_path, "1"), proc_with(tmp_path)) is None
    quiet.gate(config, sys_with(tmp_path, "1"), busy)  # never raises
    calls = []
    assert quiet.calm(config, lambda argv: calls.append(argv) or 0, proc_with(tmp_path, cgroup="/arch-update-auto.service"), sys_with(tmp_path, smt_pairs=4))
