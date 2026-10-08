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


def _stream_failing_nth(monkeypatch, nth: dict[str, int]):
    """Fail the Nth call (1-based) of a verb: the backup copy and the rollback copy are both `cp`."""
    seen: dict[str, int] = {}
    calls: list[list[str]] = []

    def fake_stream(argv, log_path, dry_run=False):
        calls.append(list(argv))
        seen[argv[1]] = seen.get(argv[1], 0) + 1
        return CommandResult(list(argv), 1 if nth.get(argv[1]) == seen[argv[1]] else 0, "")

    monkeypatch.setattr(engine, "stream_command", fake_stream)
    return calls


def test_a_rollback_whose_copy_fails_says_so_instead_of_claiming_it_rolled_back(monkeypatch, tmp_path) -> None:
    _calls_recorder(monkeypatch, tmp_path)
    calls = _stream_failing_nth(monkeypatch, {"install": 1, "cp": 2})  # the rewrite fails, then the restoring copy does
    with pytest.raises(RuntimeError) as raised:
        engine.select_kernel("lts")
    assert "rollback FAILED" in str(raised.value) and "rolled back" not in str(raised.value) and ".bak" in str(raised.value)
    assert [argv[1] for argv in calls].count("grub-mkconfig") == 1, "no menu is regenerated from a file that was not restored"


def test_a_rollback_whose_menu_cannot_be_regenerated_says_so(monkeypatch, tmp_path) -> None:
    _calls_recorder(monkeypatch, tmp_path)
    _stream_failing_nth(monkeypatch, {"install": 1, "grub-mkconfig": 2})
    with pytest.raises(RuntimeError) as raised:
        engine.select_kernel("lts")
    assert "restored from" in str(raised.value) and "regenerating" in str(raised.value) and "sudo grub-mkconfig" in str(raised.value)


def test_a_menu_without_the_kernel_is_rolled_back_truthfully_too(monkeypatch, tmp_path) -> None:
    _calls_recorder(monkeypatch, tmp_path)
    (tmp_path / "grub.cfg").write_text("linux /vmlinuz-linux\n", encoding="utf-8")
    _stream_failing_nth(monkeypatch, {"cp": 2})
    with pytest.raises(RuntimeError, match="does not reference selected kernel; the rollback FAILED too"):
        engine.select_kernel("lts")


def test_grub_defaults_that_cannot_be_read_are_a_plain_message_not_a_traceback(monkeypatch, tmp_path) -> None:
    calls = _calls_recorder(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "GRUB_DEFAULT_PATH", tmp_path / "missing" / "grub")
    with pytest.raises(RuntimeError, match="GRUB could not be read.*left untouched"):
        engine.select_kernel("lts")
    assert [argv[1] for argv in calls] == ["pacman"], "only the package was installed"


def test_the_cli_and_the_screen_report_an_oserror_plainly(monkeypatch, capsys) -> None:
    def broken(profile, dry_run=False):
        raise PermissionError(13, "Permission denied", "/etc/default/grub")

    monkeypatch.setattr(cli, "select_kernel", broken)
    assert cli.main(["kernel", "lts", "--yes"]) == 1
    assert "Permission denied" in capsys.readouterr().err
