"""Learning by witnessing: pacman.log gives a fix, it generalizes, it is shown and never run (C1, C2, C3, C9)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from arch_updater import briefs, census, witness
from test_auto import box, failed  # noqa: F401 - box is a fixture

FIXTURES = Path(__file__).parent / "fixtures" / "census"
FAILURE = (FIXTURES / "failure-2026-10-25-repo.log").read_text()
SYNTHETIC = ":: foo-1-1 and bar-2-1 are in conflict. Remove bar? [y/N] error: unresolvable package conflicts detected\n"
CONFLICT_ID = "PACMAN-REPLACEMENT-CONFLICT"


def brief_of(state_root: Path, output: str = FAILURE, at: str = "2026-10-25T18:18:14.811273+02:00", store: str = "repo", case: str = CONFLICT_ID) -> Path:
    folder = briefs.write(store, census.Case(id=case), output, "auto-20261025T181806+0200", None, "", None, None, state_root)
    meta = json.loads((folder / "meta.json").read_text())
    (folder / "meta.json").write_text(json.dumps({**meta, "at": at}))
    return folder


def settle(state_root: Path, cases=None, done=("repo",), log=FIXTURES / "pacman-2026-10-25.log"):
    cases = census.load(state_root) if cases is None else cases
    started = witness.when("2026-11-01T21:09:57+0200")
    return witness.settle(state_root, cases, set(done), started, log=log, now=witness.when("2026-11-02T10:00:00+0200"))


# ── C1: the live log of the 25th of October ─────────────────────────────────────────────────────


def test_the_live_pacman_log_of_the_25th_of_october_gives_the_hand_fix_of_the_open_brief(tmp_path) -> None:
    folder = brief_of(tmp_path)
    fresh = settle(tmp_path)
    assert len(fresh) == 1
    case = fresh[0]
    assert case.id.startswith("LOCAL-WITNESSED-") and case.repair is None and case.store == "repo"
    assert "pacman -R plasma-x11-session" in case.fix_for(FAILURE) and "--noconfirm" not in case.fix
    assert "Running 'pacman -R --noconfirm plasma-x11-session'" in case.evidence and "removed plasma-x11-session" in case.evidence
    assert case.source == "pacman.log"
    saved = [item for item in json.loads((tmp_path / "census.json").read_text()) if item["id"] == case.id]
    assert saved and saved[0]["repair"] is None
    assert json.loads((folder / "meta.json").read_text())["witnessed"] == case.id, "the brief remembers it was answered"
    assert settle(tmp_path) == [], "and is not read twice"


def test_what_the_tool_ran_itself_and_what_has_nothing_to_do_with_the_failure_is_not_the_fix(tmp_path) -> None:
    log = tmp_path / "pacman.log"
    log.write_text(
        "[2026-10-25T18:20:00+0200] [PACMAN] Running 'pacman -Syu --noconfirm'\n"
        "[2026-10-25T18:20:01+0200] [ALPM] transaction completed\n"
        "[2026-10-25T18:21:00+0200] [PACMAN] Running 'pacman -S --needed --noconfirm mpv'\n"
        "[2026-10-25T18:21:05+0200] [ALPM] transaction completed\n"
        "[2026-10-25T18:21:30+0200] [PACMAN] Running 'pacman -R --noconfirm plasma-x11-session'\n"
        "[2026-10-25T18:21:31+0200] [ALPM] transaction completed\n"
        "[2026-10-25T18:21:40+0200] [PACMAN] Running 'pacman -S --noconfirm plasma-x11-session'\n"  # typed, but never finished
    )
    brief_of(tmp_path)
    (case,) = settle(tmp_path, log=log)
    assert "mpv" not in case.fix and "Syu" not in case.fix and case.fix == "sudo pacman -R plasma-x11-session"


def test_a_store_that_did_not_pass_in_this_run_learns_nothing_yet(tmp_path) -> None:
    folder = brief_of(tmp_path)
    assert settle(tmp_path, done=()) == [] and settle(tmp_path, done=("aur",)) == []
    assert "witnessed" not in json.loads((folder / "meta.json").read_text())


def test_a_command_typed_before_the_failure_or_after_the_run_began_is_not_in_the_window(tmp_path) -> None:
    brief_of(tmp_path, at="2026-10-25T18:23:00+02:00")  # the failure came after the fix: that fix was for something else
    assert settle(tmp_path) == []


@pytest.mark.parametrize("case", [census.TOOL_REFUSED, "LOW-BATTERY"])
def test_a_refusal_or_a_check_has_no_hand_fix_to_witness(tmp_path, case) -> None:
    brief_of(tmp_path, case=case)
    assert settle(tmp_path, cases=census.load(tmp_path)) == []


def test_a_log_that_is_not_there_is_no_error(tmp_path) -> None:
    brief_of(tmp_path)
    assert settle(tmp_path, log=tmp_path / "nothing.log") == []


# ── C2: a package name shared by the failure and the command is a group ─────────────────────────


def witnessed_case(tmp_path) -> census.Case:
    brief_of(tmp_path)
    return settle(tmp_path)[0]


def test_the_case_from_the_live_log_matches_another_pair_and_names_its_package(tmp_path) -> None:
    case = witnessed_case(tmp_path)
    assert "(?P<pkg>" in case.match and case.generic == "sudo pacman -R {pkg}"
    assert case.matches(SYNTHETIC) and case.matches("foo-1-1 and bar-2-1 are in conflict")
    assert case.fix_for(SYNTHETIC) == "sudo pacman -R bar"
    assert case.fix_for(FAILURE) == "sudo pacman -R plasma-x11-session"
    assert not case.matches(":: nothing is wrong here") and not case.matches("error: target not found: bar")


def test_a_package_name_that_is_not_a_name_never_reaches_a_command(tmp_path) -> None:
    case = witnessed_case(tmp_path)
    hostile = ":: foo-1-1 and bar;rm-2-1 are in conflict"
    assert case.fix_for(hostile) == case.fix, "a value outside the package-name alphabet is not put in"
    assert case.fix_for("") == case.fix


def test_two_packages_of_the_command_are_two_groups() -> None:
    output = ":: a-1-1 and b-2-1 are in conflict. Remove b? [y/N]\n"
    general = witness.generalize(output, ["a", "b"])
    assert general is not None and set(general.groups.values()) == {"pkg", "pkg2"}
    import re

    found = re.search(general.match, ":: x-9-9 and y-8-8 are in conflict")
    assert found and found.groupdict() == {"pkg": "x", "pkg2": "y"}


def test_a_name_that_only_the_command_knows_is_not_a_fix_of_this_failure() -> None:
    assert witness.generalize("error: the flux capacitor is down\n", ["mpv"]) is None


def test_a_clause_of_nothing_but_the_name_is_too_little_to_recognise_a_failure_by() -> None:
    assert witness.generalize("bar\n", ["bar"]) is None


# ── C3: shown, never run ────────────────────────────────────────────────────────────────────────


def hand_log(path: Path, when: datetime, command: str, package: str) -> None:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.write_text(f"[{stamp}] [PACMAN] Running '{command}'\n[{stamp}] [ALPM] transaction started\n[{stamp}] [ALPM] removed {package} (1-1)\n[{stamp}] [ALPM] transaction completed\n")


def test_a_run_learns_the_hand_fix_then_meets_the_failure_and_shows_it_without_running_it(box, tmp_path) -> None:
    log = tmp_path / "pacman.log"
    box.patch.setattr(witness, "PACMAN_LOG", log)
    # First week: the update fails with a conflict; a brief is left.
    box.sh.fail["repo"] = [failed(FAILURE)]
    first = box.run()
    assert [(item["probe"], item["case"]) for item in first["trouble"]] == [("repo", CONFLICT_ID)]
    folder = Path(first["trouble"][0]["brief"])
    now = datetime.now().astimezone()
    meta = json.loads((folder / "meta.json").read_text())
    (folder / "meta.json").write_text(json.dumps({**meta, "at": (now - timedelta(hours=2)).isoformat()}))
    # In between, a person fixed it by hand; the next run passes.
    hand_log(log, now - timedelta(hours=1), "pacman -R --noconfirm plasma-x11-session", "plasma-x11-session")
    second = box.run()
    assert second["trouble"] == [] and len(second["witnessed"]) == 1
    learned = second["witnessed"][0]
    assert any(case.id == learned and case.repair is None for case in census.load(box.root))
    # Another pair of packages, another week: the same kind of failure.
    ran_before = len(box.sh.ran)
    box.sh.tools |= {"systemd-run", "kitty"}
    box.sh.fail["repo"] = [failed(SYNTHETIC)]
    third = box.run()
    (item,) = third["trouble"]
    assert item["case"] == learned and item["fix"] == "sudo pacman -R bar" and item["witnessed"] is True
    assert not [argv for argv in box.sh.ran[ran_before:] if "-R" in argv or "bar" in argv or argv[:2] == ["sh", "-c"]], "nothing ran the fix"
    assert [argv for argv in box.sh.ran[ran_before:] if argv[-2:] == ["-Syu", "--noconfirm"]], "only the update itself was tried"
    assert third["repairs"] == [{"case": learned, "repair": None, "attempted": False}]
    # Readers: the brief, the notification, the status.
    brief = (Path(item["brief"]) / "brief.md").read_text()
    assert "## A fix somebody ran by hand" in brief and "sudo pacman -R bar" in brief and "will not" in brief and f"--accept {learned}" in brief
    asked = box.sh.calls[-1]
    assert any("sudo pacman -R bar" in word and "not run for you" in word for word in asked), "the notification says what was done by hand"
    assert "--setenv=FIX=arch-update brief" in asked, "a click opens the brief and does not run the removal"
    from arch_updater import auto

    assert "sudo pacman -R bar" in auto.status()


def test_even_a_hand_edited_census_cannot_make_a_witnessed_fix_run(box) -> None:
    case = census.Case(id="LOCAL-WITNESSED-feedface", match="in conflict", repair="replay", fix="sudo pacman -R oops", store="repo")
    census.save_local([case], box.root)
    box.sh.fail["repo"] = [failed(CONFLICT_TEXT)]
    report = box.run()
    assert report["trouble"][0]["case"] == case.id
    assert report["repairs"] == [{"case": case.id, "repair": None, "attempted": False}]
    assert not [argv for argv in box.sh.ran if "oops" in argv or argv[:2] == ["sh", "-c"]]


CONFLICT_TEXT = ":: a-1-1 and b-2-1 are in conflict\nerror: failed to prepare transaction (conflicting dependencies)\n"


# ── C9: learned, then witnessed, then shipped ───────────────────────────────────────────────────


def test_recognition_asks_every_kind_of_knowledge_and_the_order_is_learned_witnessed_shipped(tmp_path) -> None:
    output = ":: foo-1-1 and bar-2-1 are in conflict. Remove bar? [y/N]\nerror: failed to prepare transaction (conflicting dependencies)\n"
    shipped = census.load(tmp_path)
    assert census.recognise(shipped, output).id == CONFLICT_ID
    witnessed = census.Case(id="LOCAL-WITNESSED-11111111", match="are in conflict", fix="sudo pacman -R bar", store="repo")
    learned = census.Case(id="LOCAL-LEARNED-22222222", match="conflicting dependencies", fix="true", repair="replay", store="repo")
    refused = "arch-update: refused: pacman -U x\n"
    # Each answers on its own...
    assert census.recognise([*shipped, witnessed], output).id == witnessed.id
    assert census.recognise([*shipped, learned], output).id == learned.id
    # ...and together the learned one wins, then the witnessed one, whatever their place in the list.
    for order in ([*shipped, witnessed, learned], [learned, witnessed, *shipped], [witnessed, *shipped, learned]):
        assert census.recognise(order, output).id == learned.id
    for order in ([*shipped, witnessed], [witnessed, *shipped]):
        assert census.recognise(order, output).id == witnessed.id
    # The shipped catalogue is the last word, and the tool's own refusal leads it.
    assert census.recognise(shipped, output + refused).id == census.TOOL_REFUSED
    assert census.recognise([*shipped, witnessed], output + refused).id == witnessed.id


def test_a_witnessed_fix_answers_only_the_store_it_was_seen_for(tmp_path) -> None:
    witnessed = census.Case(id="LOCAL-WITNESSED-11111111", match="are in conflict", fix="sudo pacman -R bar", store="repo")
    cases = [*census.load(tmp_path), witnessed]
    assert census.recognise(cases, "a-1-1 and b-2-1 are in conflict", "repo").id == witnessed.id
    assert census.recognise(cases, "a-1-1 and b-2-1 are in conflict", "aur").id == CONFLICT_ID


REPAIRED = [
    ("MIRROR-STALE-404", "wait-for-mirror", "error: failed retrieving file 'x' : The requested URL returned error: 404", "404"),
    ("ABI-MISMATCH", "rebuild-against-new-libraries", "qs: symbol lookup error: libfoo.so.3: undefined symbol bar", "symbol lookup"),
    ("DKMS-PARTIAL-MODULE-FAILURE", "rebuild-dkms", "Error! Bad return status for module build on kernel: 6.7.5 (x86_64)", "Bad return"),
    ("DNS-FAILED", "wait-for-dns", "curl: (6) Could not resolve host: geo.mirror.pkgbuild.com", "resolve host"),
    ("PACMAN-LOCK", "wait-for-lock", "error: failed to synchronize: unable to lock database", "lock database"),
    ("ROOT-SPACE-LOW", "trim-cache", "error: failed to commit transaction: no space left on device", "no space"),
]


@pytest.mark.parametrize(("shipped", "repair", "output", "word"), REPAIRED)
def test_a_witnessed_fix_never_takes_the_place_of_a_shipped_repair(tmp_path, shipped, repair, output, word) -> None:
    """After one hand fix of a failure that the tool repairs by itself, the next one is still repaired by the tool."""
    witnessed = census.Case(id="LOCAL-WITNESSED-11111111", match=word, fix="sudo pacman -Syyu", store="repo")
    shipped_cases = census.load(tmp_path)
    cases = [*shipped_cases, witnessed]
    assert census.recognise(shipped_cases, output).id == shipped
    chosen = census.recognise(cases, output, "repo")
    assert chosen.id == shipped and chosen.repair == repair


def test_a_witnessed_fix_still_wins_over_a_shipped_case_that_has_no_repair(tmp_path) -> None:
    witnessed = census.Case(id="LOCAL-WITNESSED-11111111", match="are in conflict", fix="sudo pacman -R bar", store="repo")
    assert census.recognise([*census.load(tmp_path), witnessed], SYNTHETIC).id == witnessed.id
