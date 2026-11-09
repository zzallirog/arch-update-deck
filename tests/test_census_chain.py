"""Builder pass 2: a chained fix survives `learn --accept`, and a fix stops where an undo or a reinstall begins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arch_updater import auto, briefs, census, witness
from test_auto import box, failed  # noqa: F401 - box is a fixture
from test_census_witness import FAILURE, SYNTHETIC, brief_of, settle

FAILURE2 = ":: plasma-workspace-6.8.1-1.1 and plasma-x11-session-6.7.5-3.1 are in conflict. Remove plasma-x11-session? [y/N]\n:: kwin-6.8-1 and kwin-x11-6.7.5-3.1 are in conflict. Remove kwin-x11? [y/N]\nerror: unresolvable package conflicts detected\n"
TWO = ":: foo-1-1 and bar-2-1 are in conflict. Remove bar? [y/N]\n:: baz-3-1 and qux-4-1 are in conflict. Remove qux? [y/N]\nerror: unresolvable package conflicts detected\n"


def pacman(stamp: str, command: str, packages: list[str], verb: str = "removed") -> str:
    lines = [f"[{stamp}] [PACMAN] Running '{command}'", f"[{stamp}] [ALPM] transaction started"]
    lines += [f"[{stamp}] [ALPM] {verb} {name} (1-1)" for name in packages]
    return "\n".join([*lines, f"[{stamp}] [ALPM] transaction completed", ""])


# ── a chain of two commands is one fix ──────────────────────────────────────────────────────────


def test_the_yes_is_put_into_every_pacman_command_of_a_chain_and_the_chain_is_kept() -> None:
    chained = "sudo pacman -R qux && sudo pacman -S --needed baz"
    assert witness.unattended(chained) == "sudo pacman -R --noconfirm qux && sudo pacman -S --noconfirm --needed baz"
    assert "'&&'" not in witness.unattended(chained) and "&amp;" not in witness.unattended(chained)
    assert witness.unattended("sudo pacman -R a") == "sudo pacman -R --noconfirm a"
    assert witness.unattended("sudo dkms autoinstall && sudo pacman -R a") == "sudo dkms autoinstall && sudo pacman -R --noconfirm a"
    assert witness.unattended("sudo pacman -R --noconfirm a && sudo pacman -S b") == "sudo pacman -R --noconfirm a && sudo pacman -S --noconfirm b"


def test_a_name_with_a_shell_word_in_it_stays_one_word_in_the_chain() -> None:
    assert witness.unattended("sudo pacman -R 'a b' && sudo pacman -S c") == "sudo pacman -R --noconfirm 'a b' && sudo pacman -S --noconfirm c"


@pytest.fixture
def chained(tmp_path, monkeypatch):
    """A witnessed two-command fix (remove one package, then another) and a brief for another pair of packages."""
    log = tmp_path / "pacman.log"
    log.write_text(
        pacman("2026-10-25T18:20:00+0200", "pacman -R --noconfirm plasma-x11-session", ["plasma-x11-session"])
        + pacman("2026-10-25T18:21:00+0200", "pacman -Rns --noconfirm kwin-x11", ["kwin-x11"])
    )
    failure = FAILURE2
    brief_of(tmp_path, output=failure)
    (case,) = settle(tmp_path, log=log)
    new = briefs.write("repo", census.Case(id="PACMAN-REPLACEMENT-CONFLICT"), failure, "auto-1", None, "", None, None, tmp_path)
    monkeypatch.setattr(auto, "retry_for", lambda store, where="", sh=None: ["pacman-retry"])
    return case, new


def test_a_fix_of_two_commands_is_seen_with_both_names_put_in(chained) -> None:
    case, _ = chained
    assert case.fix == "sudo pacman -R plasma-x11-session && sudo pacman -Rns kwin-x11"
    assert case.fix_for(FAILURE2) == case.fix and not case.generic, "pinned to these packages: a template would name only one"
    assert case.matches(FAILURE2) and not case.matches(TWO)


def test_accepting_a_chained_fix_runs_the_chain_once_through_sh_not_a_mangled_command(chained, tmp_path) -> None:
    case, folder = chained
    ran: list[list[str]] = []
    learned = briefs.accept(case.id, folder, state_root=tmp_path, run=lambda argv: (ran.append(argv), (0, ""))[1])
    shell = [argv[-1] for argv in ran if argv[0] == "env"]
    assert shell == [witness.unattended(case.fix)] and shell[0].count("&&") == 1 and "&amp;" not in shell[0] and "'&&'" not in shell[0]
    assert learned.fix == shell[0] and learned.repair == "replay"


def test_nothing_of_a_witnessed_text_runs_before_the_accept_step(chained, tmp_path, box) -> None:  # noqa: F811
    case, folder = chained
    census.save_local([case], box.root)
    box.sh.fail["repo"] = [failed(FAILURE2)]
    report = box.run()
    assert report["trouble"][0]["case"] == case.id and report["trouble"][0]["witnessed"] is True
    assert not [argv for argv in box.sh.ran if any(word in " ".join(argv) for word in ("-Rns", "kwin-x11", "qux")) or argv[:2] == ["sh", "-c"]]


# ── a fix stops where an undo or a reinstall begins ─────────────────────────────────────────────


def test_a_reinstall_a_week_later_is_not_part_of_the_fix(tmp_path) -> None:
    log = tmp_path / "pacman.log"
    log.write_text(
        pacman("2026-10-25T18:22:13+0200", "pacman -R --noconfirm plasma-x11-session", ["plasma-x11-session"])
        + pacman("2026-11-01T09:00:00+0200", "pacman -S --noconfirm plasma-x11-session", ["plasma-x11-session"], "installed")
    )
    brief_of(tmp_path)
    (case,) = settle(tmp_path, log=log)
    assert case.fix == "sudo pacman -R plasma-x11-session", case.fix
    assert "installed plasma-x11-session" not in case.evidence


def test_an_undo_right_after_the_removal_ends_the_fix_and_so_does_what_follows_it(tmp_path) -> None:
    log = tmp_path / "pacman.log"
    log.write_text(
        pacman("2026-10-25T18:22:13+0200", "pacman -R --noconfirm plasma-x11-session", ["plasma-x11-session"])
        + pacman("2026-10-25T18:23:00+0200", "pacman -S --noconfirm plasma-x11-session", ["plasma-x11-session"], "installed")
        + pacman("2026-10-25T18:24:00+0200", "pacman -R --noconfirm plasma-x11-session", ["plasma-x11-session"])
    )
    brief_of(tmp_path)
    (case,) = settle(tmp_path, log=log)
    assert case.fix == "sudo pacman -R plasma-x11-session"


def test_the_other_way_round_an_install_then_its_removal_stops_too() -> None:
    failure = "error: failed to prepare transaction (could not satisfy dependencies)\n:: unable to satisfy dependency 'libfoo.so=3' required by bar-2-1\n"
    run = lambda argv: (witness.read_pacman(argv), [" ".join(argv)])  # noqa: E731
    runs = [run(["pacman", "-S", "libfoo"]), run(["pacman", "-R", "libfoo"]), run(["pacman", "-S", "libfoo"])]
    case = witness.build("repo", "UNSATISFIED-DEPENDENCIES", failure, [(c, l) for c, l in runs if c], "pacman.log", "2026-10-25")
    assert case is None or case.fix == "sudo pacman -S libfoo"


def test_two_unrelated_removals_are_both_the_fix(tmp_path) -> None:
    log = tmp_path / "pacman.log"
    log.write_text(
        pacman("2026-10-25T18:20:00+0200", "pacman -R --noconfirm plasma-x11-session", ["plasma-x11-session"])
        + pacman("2026-10-25T18:21:00+0200", "pacman -Rns --noconfirm kwin-x11", ["kwin-x11"])
    )
    failure = ":: a-1-1 and plasma-x11-session-6.7.5-3.1 are in conflict. Remove plasma-x11-session? [y/N]\n:: b-1-1 and kwin-x11-6.7.5-3.1 are in conflict. Remove kwin-x11? [y/N]\n"
    brief_of(tmp_path, output=failure)
    (case,) = settle(tmp_path, log=log)
    assert case.fix == "sudo pacman -R plasma-x11-session && sudo pacman -Rns kwin-x11"


# ── the cheap ones from the tester's list ───────────────────────────────────────────────────────


def test_a_pacman_by_another_path_is_not_the_pacman_and_the_program_is_always_named_plain() -> None:
    assert witness.read_pacman(["/tmp/evil/pacman", "-R", "bar"]) is None and witness.read_pacman(["./pacman", "-R", "bar"]) is None
    read = witness.read_pacman(["/usr/bin/pacman", "-R", "bar"])
    assert read is not None and witness.fix_of(read.argv) == "sudo pacman -R bar"
    assert witness.fix_of(["/usr/local/bin/pacman", "-R", "bar"]) == "sudo pacman -R bar", "never a path in what a person is shown"


def test_one_huge_command_or_a_log_of_thousands_cannot_stall_a_run() -> None:
    assert witness.read_pacman(["pacman", "-R", *[f"pkg{n}" for n in range(witness.MAX_TARGETS + 1)]]) is None
    assert witness.read_pacman(["pacman", "-R", *[f"pkg{n}" for n in range(witness.MAX_TARGETS)]]) is not None
    text = "".join(pacman(f"2026-10-25T18:{n // 60:02d}:{n % 60:02d}+0200", f"pacman -R --noconfirm pkg{n}", [f"pkg{n}"]) for n in range(1000))
    hands = witness.hand_commands(text, witness.when("2026-10-24T00:00:00+0200"), witness.when("2026-10-26T00:00:00+0200"))
    assert len(hands) == witness.MAX_COMMANDS and hands[-1].argv[-1] == "pkg999", "the newest ones"


def test_looking_at_the_census_rewrites_nothing(tmp_path, monkeypatch, capsys) -> None:
    from arch_updater import day, main

    junk = (Path(__file__).parent / "fixtures" / "census" / "census-2026-11-01.json").read_text()
    (tmp_path / "census.json").write_text(junk)
    for module in (main, day):
        monkeypatch.setattr(module, "STATE_ROOT", tmp_path)
    for argv in (["census"], ["census", "--stats"], ["census", "--share", "--json"]):
        main.main(argv)
        assert (tmp_path / "census.json").read_text() == junk, argv
    day.share_offer(None)
    assert (tmp_path / "census.json").read_text() == junk, "nor does the line of Update Day"


def test_the_notice_says_a_fix_was_seen_not_learned_when_nobody_taught_it() -> None:
    base = {"exit_code": 0, "state": "IDLE", "crash": None, "trouble": [], "updated": {}, "held": {}}
    calls: list[list[str]] = []

    class Sh:
        def has(self, tool):
            return tool == "notify-send"

        def capture(self, argv, timeout=0):
            calls.append(argv)

        def terminal(self):
            return None

    auto.announce(Sh(), {**base, "learned_unshared": 1, "witnessed_unshared": 1})
    assert "saw 1 fix done by hand (it does not run it)" in calls[-1][-1] and "learned" not in calls[-1][-1]
    auto.announce(Sh(), {**base, "learned_unshared": 3, "witnessed_unshared": 1})
    assert "learned to fix 2 things on its own and saw 1 fix done by hand" in calls[-1][-1]
    auto.announce(Sh(), {**base, "learned_unshared": 1, "witnessed_unshared": 0})
    assert "learned to fix 1 thing on its own" in calls[-1][-1]


def test_a_click_on_a_community_fix_opens_the_brief_and_does_not_run_the_fix() -> None:
    item = {"probe": "repo", "case": "CENSUS-ABCD1234", "detail": "x", "fix": "sudo pacman -R bar", "community": True}
    calls: list[list[str]] = []

    class Sh:
        def has(self, tool):
            return tool in ("notify-send", "systemd-run")

        def capture(self, argv, timeout=0):
            calls.append(argv)

        def terminal(self):
            return ["kitty"]

    auto.announce(Sh(), {"exit_code": 30, "state": "RECOVERY_PENDING", "crash": None, "trouble": [item], "updated": {}, "held": {}})
    assert "--setenv=FIX=arch-update brief" in calls[-1] and not any("FIX=sudo pacman" in word for word in calls[-1])
    assert "read it before you run it" in calls[-1][-1]
