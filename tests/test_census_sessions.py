"""The second source: assistants' transcripts feed the census from the weekly run (C5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from arch_updater import auto, census, power, transcripts, witness
from test_auto import box, failed  # noqa: F401 - box is a fixture

SESSION = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
FAILING = ":: foo-1-1 and bar-2-1 are in conflict. Remove bar? [y/N]\nerror: unresolvable package conflicts detected\nerror: failed to prepare transaction (conflicting dependencies)\n"
NEW = ":: baz-3-1 and qux-4-1 are in conflict. Remove qux? [y/N] error: unresolvable package conflicts detected\n"


def transcript(root: Path, rows: list[tuple[str, str, str, bool]]) -> Path:
    """A Claude transcript: (id, command, result text, is_error) per tool call, the way the scan reads them."""
    root.mkdir(parents=True, exist_ok=True)
    lines = []
    for number, (call, command, result, error) in enumerate(rows):
        stamp = f"2026-11-0{number + 1}T10:00:00.000Z"
        lines.append(json.dumps({"type": "assistant", "sessionId": SESSION, "timestamp": stamp, "message": {"content": [{"type": "tool_use", "id": call, "input": {"command": command}}]}}))
        lines.append(json.dumps({"type": "user", "sessionId": SESSION, "timestamp": stamp, "message": {"content": [{"type": "tool_result", "tool_use_id": call, "content": result, "is_error": error}]}}))
    path = root / f"{SESSION}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def proven_fix(root: Path) -> Path:
    return transcript(root, [
        ("t1", "sudo pacman -S --noconfirm foo 2>&1 | tail -20", FAILING, True),
        ("t2", "sudo pacman -R --noconfirm bar 2>&1 | tail -5", "checking dependencies...\nremoving bar\n", False),
    ])


def test_a_transcript_with_a_proven_fix_after_a_failure_becomes_a_witnessed_case(tmp_path) -> None:
    proven_fix(tmp_path / "claude")
    cases = census.load(tmp_path)
    done = transcripts.refresh(tmp_path, cases, claude_root=tmp_path / "claude", codex_root=tmp_path / "none", log=tmp_path / "no.log")
    assert done["ran"] is True and done["proven_sessions"] == 1 and len(done["witnessed"]) == 1
    saved = [item for item in json.loads((tmp_path / "census.json").read_text()) if item["id"] == done["witnessed"][0]]
    assert saved and saved[0]["fix"] == "sudo pacman -R bar" and saved[0]["repair"] is None and "claude session" in saved[0]["source"]
    (case,) = [item for item in census.load(tmp_path) if item.witnessed]
    assert case.matches(NEW) and case.fix_for(NEW) == "sudo pacman -R qux", "and it answers the next pair"
    assert (tmp_path / "reports" / "failure-evidence.json").is_file() and (tmp_path / "reports" / "update-sessions.json").is_file()


def test_a_fix_that_names_nothing_of_the_failure_or_changed_nothing_is_not_kept(tmp_path) -> None:
    transcript(tmp_path / "claude", [
        ("t1", "sudo pacman -S --noconfirm foo", FAILING, True),
        ("t2", "sudo pacman -S --noconfirm mpv", "installing mpv\n", False),  # proven, but about something else
        ("t3", "sudo pacman -R --noconfirm bar", "", True),  # about the failure, but it failed
    ])
    done = transcripts.refresh(tmp_path, census.load(tmp_path), claude_root=tmp_path / "claude", codex_root=tmp_path / "none", log=tmp_path / "no.log")
    assert done["ran"] is True and done["witnessed"] == []


def test_a_week_old_scan_is_not_repeated_and_a_stale_one_is(tmp_path) -> None:
    proven_fix(tmp_path / "claude")
    args = {"claude_root": tmp_path / "claude", "codex_root": tmp_path / "none", "log": tmp_path / "no.log"}
    assert transcripts.refresh(tmp_path, census.load(tmp_path), **args)["ran"] is True
    assert transcripts.refresh(tmp_path, census.load(tmp_path), **args) == {"ran": False, "fresh": True}
    later = datetime.now().astimezone() + timedelta(days=8)
    assert transcripts.refresh(tmp_path, census.load(tmp_path), now=later, **args)["ran"] is True


def test_on_a_battery_the_scan_waits_and_says_why(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(power, "heavy_hold", lambda sys_root=None: "the machine runs on its battery (50%); heavy work waits for the charger")
    proven_fix(tmp_path / "claude")
    done = transcripts.refresh(tmp_path, census.load(tmp_path), claude_root=tmp_path / "claude", codex_root=tmp_path / "none", log=tmp_path / "no.log")
    assert done == {"ran": False, "held": "the machine runs on its battery (50%); heavy work waits for the charger"}
    assert not (tmp_path / "reports").exists() and not (tmp_path / "census.json").exists()


def test_the_weekly_run_is_the_actor_and_the_census_is_the_reader(box, monkeypatch) -> None:
    proven_fix(box.root / "claude")
    monkeypatch.setattr(transcripts, "CLAUDE_ROOT", box.root / "claude")
    monkeypatch.setattr(transcripts, "CODEX_ROOT", box.root / "none")
    monkeypatch.setattr(transcripts, "PACMAN_LOG", box.root / "no.log")
    first = box.run()
    assert first["sessions"]["ran"] is True and len(first["sessions"]["witnessed"]) == 1
    assert box.run()["sessions"] == {"ran": False, "fresh": True}, "once a week"
    # The reader: a failure of the same kind, with other packages, now shows what was done in that session, and runs nothing.
    box.sh.fail["repo"] = [failed(NEW)]
    report = box.run()
    (item,) = report["trouble"]
    assert item["witnessed"] is True and item["fix"] == "sudo pacman -R qux"
    assert not [argv for argv in box.sh.ran if "qux" in argv]


def test_a_dry_run_scans_nothing(box, monkeypatch) -> None:
    proven_fix(box.root / "claude")
    monkeypatch.setattr(transcripts, "CLAUDE_ROOT", box.root / "claude")
    report = box.run(dry_run=True)
    assert report["sessions"] == {"ran": False} and not (box.root / "reports").exists()


def test_the_scan_command_keeps_what_it_finds_too(tmp_path, monkeypatch, capsys) -> None:
    from arch_updater import main

    proven_fix(tmp_path / "claude")
    monkeypatch.setattr(transcripts, "CLAUDE_ROOT", tmp_path / "claude")
    monkeypatch.setattr(transcripts, "CODEX_ROOT", tmp_path / "none")
    monkeypatch.setattr(transcripts, "PACMAN_LOG", tmp_path / "no.log")
    assert main.main(["scan-sessions", "--output-dir", str(tmp_path / "out")]) == 0
    assert "kept 1 fix" in capsys.readouterr().err
    assert any(case.witnessed for case in census.load(main.STATE_ROOT))
