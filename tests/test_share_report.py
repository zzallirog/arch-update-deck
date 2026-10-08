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


def test_a_shared_census_carries_what_was_learned_and_not_who_learned_it() -> None:
    from arch_updater.census import Case
    from arch_updater.share_report import share_census

    cases = [
        Case(id="ROOT-SPACE-LOW", match="no space", fix="sudo paccache -rk2"),
        Case(id="LOCAL-LEARNED-ab12", match="theme .* not found", fix="cp /home/alice/themes/x ~/.themes/", meaning="dotfiles:/home/alice/dots: theme missing",
             store="dotfiles:/home/alice/dots", where="/home/alice/dots", seen=3),
        Case(id="LOCAL-0f0f", match="error: alice\x1b]52;c;evil\x07 broke", fix="arch-update brief"),
    ]
    shared = share_census(cases, "/home/alice", "alice")
    text = repr(shared)
    assert shared["schema"] == "arch-update-census/v1" and [case["id"] for case in shared["cases"]] == ["LOCAL-LEARNED-ab12", "LOCAL-0f0f"]
    assert "/home/alice" not in text and "alice" not in text and "\x1b" not in text
    assert shared["cases"][0]["fix"] == "cp ~/themes/x ~/.themes/" and shared["cases"][0]["seen"] == 3


def test_the_share_button_fills_an_issue_and_never_sends_it() -> None:
    from urllib.parse import unquote

    from arch_updater.share_report import UPSTREAM, URL_BUDGET, issue_url

    small = {"schema": "arch-update-census/v1", "cases": [{"id": "LOCAL-LEARNED-1", "fix": "true"}, {"id": "LOCAL-2", "fix": "arch-update brief"}]}
    url = issue_url(small)
    assert url.startswith(f"{UPSTREAM}/issues/new?title=") and "1 learned fixes, 1 new failures" in unquote(url) and "LOCAL-LEARNED-1" in unquote(url)
    big = {"schema": "arch-update-census/v1", "cases": [{"id": f"LOCAL-{n}", "match": "x" * 200} for n in range(100)]}
    long = issue_url(big)
    assert len(long) <= URL_BUDGET and "attach the output" in unquote(long)


def test_what_was_handed_upstream_is_marked_and_not_handed_again(tmp_path) -> None:
    from arch_updater.census import Case
    from arch_updater.share_report import mark, mark_sent, unsent

    cases = [Case(id="LOCAL-LEARNED-a", fix="true"), Case(id="LOCAL-b"), Case(id="ROOT-SPACE-LOW")]
    assert [case.id for case in unsent(cases, tmp_path)] == ["LOCAL-LEARNED-a", "LOCAL-b"]  # as much as there is
    assert [mark(case, tmp_path) for case in cases] == ["[ ]", "[ ]", "*  "]
    mark_sent(unsent(cases, tmp_path), tmp_path)
    assert unsent(cases, tmp_path) == [] and [mark(case, tmp_path) for case in cases] == ["[x]", "[x]", "*  "]
    assert [case.id for case in unsent([*cases, Case(id="LOCAL-LEARNED-c", fix="x")], tmp_path)] == ["LOCAL-LEARNED-c"]


def test_each_learned_fix_is_offered_once_and_share_false_offers_none(tmp_path) -> None:
    from arch_updater.census import Case
    from arch_updater.share_report import mark_sent, should_offer

    cases = [Case(id="LOCAL-LEARNED-a", fix="true"), Case(id="LOCAL-b")]
    assert should_offer(cases, tmp_path, {"share": False}) == []
    assert [case.id for case in should_offer(cases, tmp_path)] == ["LOCAL-LEARNED-a"]
    assert should_offer(cases, tmp_path) == []  # once
    later = [*cases, Case(id="LOCAL-LEARNED-c", fix="x")]
    mark_sent([later[2]], tmp_path)
    assert should_offer(later, tmp_path) == []  # already handed upstream


def test_the_shipped_catalogue_is_official_and_a_shared_case_is_local() -> None:
    import json

    from arch_updater.census import Case
    from arch_updater.engine import VAULT_PATH
    from arch_updater.share_report import share_census

    assert {mode.get("origin") for mode in json.loads(VAULT_PATH.read_text())["modes"]} == {"official"}
    assert share_census([Case(id="LOCAL-1")], "", "")["cases"][0]["origin"] == "local"
