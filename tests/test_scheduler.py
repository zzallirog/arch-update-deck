import json
from types import SimpleNamespace

import conftest
from arch_updater import scheduler as scheduler_module
from arch_updater.scheduler import run_patrol, scheduler_status


def test_scheduler_status_reads_its_own_timer_properties(monkeypatch) -> None:
    outputs = iter(
        [
            SimpleNamespace(returncode=0, output="running\n"),
            SimpleNamespace(
                returncode=0,
                output=(
                    "LoadState=loaded\nActiveState=active\nSubState=waiting\n"
                    "NextElapseUSecRealtime=Tue 2026-09-01 09:30:00 EEST\n"
                    "LastTriggerUSec=Mon 2026-08-31 09:34:00 EEST\n"
                ),
            ),
        ]
    )
    monkeypatch.setattr("arch_updater.scheduler.run_capture", lambda *_args, **_kwargs: next(outputs))
    result = scheduler_status()
    assert result["patrol"]["active"] is True
    assert result["patrol"]["next"] == "Tue 2026-09-01 09:30:00 EEST"


def test_scheduler_status_reports_systemd_failure(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.scheduler.run_capture", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, output="no user manager"))
    result = scheduler_status()
    assert result["available"] is False
    assert "no user manager" in result["manager"]


def _stub_patrol(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.scheduler.snapshot", lambda **_kwargs: {"updates": {"repo": 2, "aur": 1}})
    monkeypatch.setattr("arch_updater.scheduler.attest", lambda **_kwargs: {"healthy": True, "issues": [], "reboot_required": False})
    monkeypatch.setattr("arch_updater.scheduler.scan_cves", lambda: {"available": True, "findings": []})


def test_patrol_records_a_read_only_verification(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr("arch_updater.scheduler.STATE_ROOT", state)
    monkeypatch.setattr("arch_updater.surface.HISTORY_PATH", state / "patrol-history.json")
    _stub_patrol(monkeypatch)
    report = run_patrol()
    assert report["kind"] == "read-only-patrol"
    assert report["verification"]["healthy"] is True
    assert (state / "last-patrol.json").is_file()
    history = json.loads((state / "patrol-history.json").read_text())
    assert history[0]["pending"] == {"repo": 2, "aur": 1}, "the history entry landed in the test's own state"


def test_a_patrol_test_leaves_the_real_state_untouched(monkeypatch) -> None:
    def fingerprint():
        return [(path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(conftest.REAL_STATE.glob("*.json"))] if conftest.REAL_STATE.is_dir() else []

    before = fingerprint()
    _stub_patrol(monkeypatch)
    run_patrol()
    assert fingerprint() == before, "run_patrol wrote into the real ~/.local/state"
    assert str(conftest.REAL_STATE) not in str(scheduler_module.STATE_ROOT)


def test_scheduler_status_returns_a_patrol_summary_not_the_snapshot(tmp_path, monkeypatch) -> None:
    (tmp_path / "last-patrol.json").write_text(
        '{"kind":"read-only-patrol","finished_at":"now","snapshot":{"updates":{"repo":2,"aur":1},"huge":"omit"},"verification":{"healthy":true,"reboot_required":true},"cve":{"available":true,"findings":[{"package":"one"},{"package":"two"}]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("arch_updater.scheduler.STATE_ROOT", tmp_path)
    outputs = iter(
        [
            SimpleNamespace(returncode=0, output="running\n"),
            SimpleNamespace(returncode=1, output="Unit arch-update-patrol.timer could not be found.\n"),
        ]
    )
    monkeypatch.setattr("arch_updater.scheduler.run_capture", lambda *_args, **_kwargs: next(outputs))
    result = scheduler_status()
    assert result["last_patrol"] == {
        "started_at": None,
        "finished_at": "now",
        "kind": "read-only-patrol",
        "healthy": True,
        "reboot_required": True,
        "pending": {"repo": 2, "aur": 1},
        "cve_available": True,
        "cve_findings": 2,
        "snapshot_error": None,
        "cve_error": None,
    }
    assert "findings" not in result["last_patrol"]
