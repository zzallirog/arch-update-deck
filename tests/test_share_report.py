import json

from arch_updater.main import main
from arch_updater.share_report import sanitize_patrol_report


FORBIDDEN_VALUES = [
    "workstation.lan",
    "/var/lib/pacman/db.lck",
    "/etc/pacman.conf",
    "linux",
    "openssl",
    "visual-studio-code-bin",
    "CVE-2024-21626",
    "runc",
    "pacman -Syu --noconfirm",
    "journalctl -u sshd",
    "ssh-ed25519 AAAAEXAMPLEKEY",
    "arbitrary-secret-field",
    "root@workstation.lan",
]


def _realistic_report() -> dict:
    return {
        "finished_at": "2026-03-14T09:41:00+00:00",
        "hostname": "workstation.lan",
        "user": "root@workstation.lan",
        "snapshot": {
            "updates": {
                "repo": [
                    {"name": "linux", "old": "6.12.8.arch1-1", "new": "6.12.9.arch1-1"},
                    {"name": "openssl", "old": "3.4.0-1", "new": "3.4.1-1"},
                ],
                "aur": [{"name": "visual-studio-code-bin", "old": "1.96.0-1", "new": "1.96.2-1"}],
            }
        },
        "cve": {
            "available": True,
            "findings": [{"id": "CVE-2024-21626", "package": "runc", "severity": "critical"}],
        },
        "verification": {
            "healthy": False,
            "reboot_required": True,
            "issues": [
                {"mode": "command-failed", "command": "pacman -Syu --noconfirm", "path": "/var/lib/pacman/db.lck"},
                {"mode": "command-failed", "command": "journalctl -u sshd", "path": "/etc/pacman.conf"},
                {"mode": "offline-mirror", "path": "/etc/pacman.conf"},
            ],
        },
        "commands": ["pacman -Syu --noconfirm", "journalctl -u sshd"],
        "paths": ["/var/lib/pacman/db.lck", "/etc/pacman.conf"],
        "ssh_key": "ssh-ed25519 AAAAEXAMPLEKEY",
        "notes": "arbitrary-secret-field",
    }


def test_sanitize_patrol_report_keeps_only_the_allowed_shape() -> None:
    out = sanitize_patrol_report(_realistic_report())

    assert out == {
        "schema": "arch-update-share/v1",
        "generated_at": "2026-03-14T09:41:00+00:00",
        "summary": {
            "healthy": False,
            "reboot_required": True,
            "pending": {"repo": 2, "aur": 1},
            "cve_available": True,
            "cve_findings": 1,
            "issue_modes": ["command-failed", "offline-mirror"],
        },
    }

    dumped = json.dumps(out)
    for value in FORBIDDEN_VALUES:
        assert value not in dumped


def test_share_report_command_needs_a_local_report(monkeypatch, capsys) -> None:
    monkeypatch.setattr("arch_updater.main.last_patrol_report", lambda: None)

    assert main(["share-report"]) == 1
    assert "no local patrol report" in capsys.readouterr().err


def test_share_report_command_prints_sanitized_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("arch_updater.main.last_patrol_report", _realistic_report)

    assert main(["share-report"]) == 0
    output = capsys.readouterr().out
    assert "arch-update-share/v1" in output
    assert "workstation.lan" not in output
    assert "CVE-2024-21626" not in output
