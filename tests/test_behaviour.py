"""Behaviour that outlives the screens: patrol history, what a shared report may carry, the failure vault."""

from __future__ import annotations

import json
from pathlib import Path

from arch_updater import repairs
from arch_updater.engine import VAULT_PATH, load_json
from arch_updater.share_report import sanitize_patrol_report
from arch_updater.surface import HISTORY_CAP, append_patrol_history, load_patrol_history


def test_patrol_history_is_capped_and_a_broken_file_reads_as_empty(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "patrol-history.json"
    monkeypatch.setattr("arch_updater.surface.HISTORY_PATH", path)
    path.write_text("{not json", encoding="utf-8")
    assert load_patrol_history() == []
    for index in range(20):
        append_patrol_history({"finished_at": str(index), "healthy": True, "pending": {"repo": 0, "aur": 0}, "cve_findings": 0})
    history = load_patrol_history()
    assert len(history) == HISTORY_CAP
    assert history[0]["finished_at"] == "19"


def test_a_shared_report_carries_no_identity() -> None:
    report = {
        "finished_at": "now",
        "hostname": "private-host",
        "snapshot": {"updates": {"repo": 1, "aur": 2}},
        "verification": {"healthy": True, "reboot_required": False, "issues": []},
        "cve": {"available": True, "findings": [{"package": "secret-package"}]},
    }
    text = json.dumps(sanitize_patrol_report(report))
    assert "private-host" not in text and "secret-package" not in text


def test_every_automatic_vault_mode_has_a_repair_that_exists() -> None:
    modes = load_json(VAULT_PATH)["modes"]
    assert modes
    for mode in modes:
        repair = mode.get("repair")
        assert repair is None or repair in repairs.ACTIONS, f"{mode['id']} names a repair that does not exist: {repair}"
        assert bool(mode.get("automatic")) == (repair is not None), f"{mode['id']}: automatic and repair disagree"


def test_no_fix_in_the_vault_points_at_a_command_that_is_gone() -> None:
    from arch_updater.main import build_parser

    commands = set(build_parser()._subparsers._group_actions[0].choices)
    for mode in load_json(VAULT_PATH)["modes"]:
        words = (mode.get("fix") or "").split()
        if words[:1] == ["arch-update"] and len(words) > 1 and not words[1].startswith("-"):
            assert words[1] in commands, f"{mode['id']}: `{mode['fix']}` is not a command any more"
