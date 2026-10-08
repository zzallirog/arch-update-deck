"""Kernel selection: confirmation on the CLI and the GRUB backup/rollback order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arch_updater import engine, main as cli
from arch_updater.engine import CommandResult


def _calls_recorder(monkeypatch, tmp_path: Path, failing: tuple[str, ...] = ()):
    grub = tmp_path / "grub"
    grub.write_text('GRUB_DEFAULT=0\n', encoding="utf-8")
    cfg = tmp_path / "grub.cfg"
    cfg.write_text("linux /vmlinuz-linux-lts\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_stream(argv, log_path, dry_run=False):
        calls.append(list(argv))
        verb = argv[1]
        return CommandResult(list(argv), 1 if verb in failing else 0, "")

    monkeypatch.setattr(engine, "GRUB_DEFAULT_PATH", grub)
    monkeypatch.setattr(engine, "GRUB_CFG_PATH", cfg)
    monkeypatch.setattr(engine, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(engine, "privilege_command", lambda: "sudo")
    monkeypatch.setattr(engine, "stream_command", fake_stream)
    monkeypatch.setattr(engine, "available_kernel_profiles", lambda: [{"id": "lts", "package": "linux-lts", "headers": "linux-lts-headers"}])
    monkeypatch.setattr(engine, "detect_kernel", lambda: {"bootloader": "grub", "running_package": "linux"})
    return calls


def test_failed_grub_backup_stops_before_anything_is_rewritten(monkeypatch, tmp_path) -> None:
    calls = _calls_recorder(monkeypatch, tmp_path, failing=("cp",))
    with pytest.raises(RuntimeError, match="GRUB was left untouched"):
        engine.select_kernel("lts")
    verbs = [argv[1] for argv in calls]
    assert "install" not in verbs
    assert "grub-mkconfig" not in verbs
    assert verbs.count("cp") == 1  # no rollback copy of a backup that was never made


def test_failed_rewrite_rolls_back_from_the_backup(monkeypatch, tmp_path) -> None:
    calls = _calls_recorder(monkeypatch, tmp_path, failing=("install",))
    with pytest.raises(RuntimeError, match="rolled back"):
        engine.select_kernel("lts")
    copies = [argv for argv in calls if argv[1] == "cp"]
    assert len(copies) == 2
    assert copies[1][-1] == str(engine.GRUB_DEFAULT_PATH)


def test_kernel_cli_refuses_without_a_terminal_unless_yes(monkeypatch, capsys) -> None:
    selected: list[str] = []
    monkeypatch.setattr(cli, "select_kernel", lambda profile, dry_run=False: selected.append(profile) or {"changed": False})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    assert cli.main(["kernel", "lts"]) == 1
    assert selected == []
    assert "--yes" in capsys.readouterr().err

    assert cli.main(["kernel", "lts", "--yes"]) == 0
    assert selected == ["lts"]
    assert json.loads(capsys.readouterr().out) == {"changed": False}


def test_kernel_cli_declined_confirmation_installs_nothing(monkeypatch) -> None:
    selected: list[str] = []
    monkeypatch.setattr(cli, "select_kernel", lambda profile, dry_run=False: selected.append(profile) or {})
    monkeypatch.setattr(cli, "_confirm_kernel", lambda profile: False)
    assert cli.main(["kernel", "lts"]) == 1
    assert selected == []
