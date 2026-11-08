"""`arch-update learn --accept ID`: a fix somebody ran by hand becomes a replay, once it has been proven on a failure (C4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arch_updater import auto, briefs, census, main, witness
from test_auto import box, failed  # noqa: F401 - box is a fixture
from test_census_witness import SYNTHETIC, brief_of, settle, FAILURE

NEW_FAILURE = SYNTHETIC + "error: failed to prepare transaction (conflicting dependencies)\n"


@pytest.fixture
def witnessed(tmp_path, monkeypatch):
    """A witnessed fix from the live log, and an open brief for another pair of packages that it answers."""
    brief_of(tmp_path)
    (case,) = settle(tmp_path)
    folder = briefs.write("repo", census.Case(id="PACMAN-REPLACEMENT-CONFLICT"), NEW_FAILURE, "auto-20261109T100000+0200", None, "", None, None, tmp_path)
    ran: list[list[str]] = []
    retries = {"argv": ["pacman-retry"]}
    monkeypatch.setattr(auto, "retry_for", lambda store, where="", sh=None: retries["argv"])
    return case, folder, ran


def run_ok(ran):
    return lambda argv: (ran.append(argv), (0, ""))[1]


def test_accept_runs_the_fix_for_this_failure_redoes_the_step_and_keeps_a_replay(witnessed, tmp_path) -> None:
    case, folder, ran = witnessed
    learned = briefs.accept(case.id, state_root=tmp_path, run=run_ok(ran))
    assert [argv[-1] if argv[0] == "env" else argv for argv in ran] == ["sudo pacman -R --noconfirm bar", ["pacman-retry"]], "the fix for bar, then the step"
    assert learned.id.startswith(census.LEARNED) and learned.repair == "replay" and learned.fix == "sudo pacman -R --noconfirm bar"
    assert learned.store == "repo" and learned.matches(NEW_FAILURE) and not learned.matches(NEW_FAILURE.replace("bar", "baz"))
    cases = census.load(tmp_path)
    assert case.id not in [item.id for item in cases], "the witnessed one is replaced"
    assert census.recognise(cases, NEW_FAILURE, "repo").id == learned.id
    assert json.loads((folder / "meta.json").read_text())["learned"] == learned.id


def test_accept_keeps_nothing_when_the_step_still_fails(witnessed, tmp_path) -> None:
    case, folder, ran = witnessed

    def run(argv):
        ran.append(argv)
        return (1, "still broken") if argv == ["pacman-retry"] else (0, "")

    with pytest.raises(ValueError, match="still fails"):
        briefs.accept(case.id, state_root=tmp_path, run=run)
    assert [item.id for item in census.load(tmp_path) if item.witnessed] == [case.id], "it stays witnessed"
    assert not [item for item in census.load(tmp_path) if item.learned]


def test_accept_refuses_a_failure_the_fix_does_not_answer_and_a_case_that_is_not_witnessed(witnessed, tmp_path) -> None:
    case, folder, ran = witnessed
    other = briefs.write("repo", census.Case(id="LOCAL-1"), "error: the flux capacitor is down\n", "auto-x", None, "", None, None, tmp_path)
    with pytest.raises(ValueError, match="not one this fix answers"):
        briefs.accept(case.id, other, state_root=tmp_path, run=run_ok(ran))
    with pytest.raises(ValueError, match="not a witnessed fix"):
        briefs.accept("ROOT-SPACE-LOW", state_root=tmp_path, run=run_ok(ran))
    assert ran == []


def test_the_next_run_replays_the_accepted_fix_by_itself(box, tmp_path) -> None:
    box.patch.setattr(auto, "retry_for", lambda store, where="", sh=None: None)
    brief_of(box.root)
    (case,) = settle(box.root)
    folder = briefs.write("repo", census.Case(id="PACMAN-REPLACEMENT-CONFLICT"), NEW_FAILURE, "auto-1", None, "", None, None, box.root)
    briefs.accept(case.id, state_root=box.root, run=lambda argv: (0, ""))
    box.sh.fail["repo"] = [failed(NEW_FAILURE.replace("bar", "bar"))]
    report = box.run()
    assert report["state"] == "IDLE" and len(report["self_fixed"]) == 1
    replayed = [argv for argv in box.sh.ran if argv[:3] == ["env", "-C", str(Path("~").expanduser())] or argv[-1:] == ["sudo pacman -R --noconfirm bar"]]
    assert replayed and replayed[0][-1] == "sudo pacman -R --noconfirm bar"
    assert report["repairs"][0]["repair"] == "replay" and report["repairs"][0]["attempted"] is True


def test_unattended_puts_the_yes_in_once_and_only_for_pacman() -> None:
    assert witness.unattended("sudo pacman -R bar") == "sudo pacman -R --noconfirm bar"
    assert witness.unattended("sudo pacman -Rns bar") == "sudo pacman -Rns --noconfirm bar"
    assert witness.unattended("sudo pacman -R --noconfirm bar") == "sudo pacman -R --noconfirm bar"
    assert witness.unattended("sudo dkms autoinstall") == "sudo dkms autoinstall"


def test_the_command_asks_first_and_runs_nothing_without_a_terminal(witnessed, tmp_path, monkeypatch, capsys) -> None:
    case, folder, ran = witnessed
    monkeypatch.setattr(briefs, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(briefs, "_run", lambda argv: (ran.append(argv), (0, ""))[1])
    code = main.main(["learn", "--accept", case.id])
    assert code != 0 and ran == [], "no terminal, no --yes: nothing runs"
    err = capsys.readouterr()
    assert "sudo pacman -R --noconfirm bar" in err.out and "--yes" in err.err
    assert main.main(["learn", "--accept", case.id, "--yes"]) == 0
    assert [argv[-1] for argv in ran if argv[0] == "env"] == ["sudo pacman -R --noconfirm bar"]


def test_learn_still_needs_its_three_arguments_without_accept(capsys) -> None:
    assert main.main(["learn", "--match", "x"]) != 0
    assert "--accept" in capsys.readouterr().err
