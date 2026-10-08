from types import SimpleNamespace

from arch_updater import scheduler
from arch_updater.scheduler import PATROL_UNIT, install_daily_patrol_timer, remove_timer


def _systemctl(monkeypatch):
    seen = []

    def fake_run(argv, **_kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, output="")

    monkeypatch.setattr("arch_updater.scheduler.run_capture", fake_run)
    monkeypatch.setattr(scheduler.shutil, "which", lambda tool: "/opt/deck/bin/arch-update")
    return seen


def test_daily_patrol_is_a_persistent_unit_pair_not_a_transient_timer(tmp_path, monkeypatch) -> None:
    seen = _systemctl(monkeypatch)
    result = install_daily_patrol_timer(unit_dir=tmp_path)
    assert result["installed"] is True and result["unit"] == "arch-update-patrol.timer"
    service = (tmp_path / f"{PATROL_UNIT}.service").read_text()
    timer = (tmp_path / f"{PATROL_UNIT}.timer").read_text()
    assert "ExecStart=/opt/deck/bin/arch-update patrol\n" in service
    assert "OnCalendar=*-*-* 09:30:00\n" in timer and "Persistent=true\n" in timer
    assert "RandomizedDelaySec=15min\n" in timer and "WantedBy=timers.target\n" in timer
    assert ["systemctl", "--user", "enable", "--now", "arch-update-patrol.timer"] in seen
    assert not any(argv[0] == "systemd-run" for argv in seen)


def test_installing_the_patrol_twice_leaves_one_identical_pair(tmp_path, monkeypatch) -> None:
    _systemctl(monkeypatch)
    tmp_path = tmp_path / "units"
    install_daily_patrol_timer(unit_dir=tmp_path)
    first = sorted((path.name, path.read_text()) for path in tmp_path.iterdir())
    install_daily_patrol_timer(unit_dir=tmp_path)
    assert sorted((path.name, path.read_text()) for path in tmp_path.iterdir()) == first
    assert [name for name, _ in first] == ["arch-update-patrol.service", "arch-update-patrol.timer"]


def test_removing_a_timer_deletes_its_files_and_is_safe_to_repeat(tmp_path, monkeypatch) -> None:
    seen = _systemctl(monkeypatch)
    tmp_path = tmp_path / "units"
    install_daily_patrol_timer(unit_dir=tmp_path)
    assert remove_timer(PATROL_UNIT, unit_dir=tmp_path)["removed"] is True
    assert list(tmp_path.iterdir()) == []
    assert ["systemctl", "--user", "disable", "--now", "arch-update-patrol.timer"] in seen
    assert remove_timer(PATROL_UNIT, unit_dir=tmp_path)["removed"] is True
