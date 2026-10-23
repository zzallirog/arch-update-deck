"""Identical .pacnew files go by themselves, through a shim that never leaves /etc; the rest only get a state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from arch_updater import auto, day, pacnew, pacnew_prune
from test_auto import box, ok  # noqa: F401 - the scripted machine, as a fixture

ME = os.getuid()


@pytest.fixture
def etc(tmp_path):
    folder = tmp_path / "etc"
    folder.mkdir(mode=0o755)
    return folder


def pair(folder: Path, name: str, live: bytes | None, new: bytes) -> Path:
    if live is not None:
        (folder / name).write_bytes(live)
    (folder / f"{name}.pacnew").write_bytes(new)
    return folder / f"{name}.pacnew"


# ── the shim: what it deletes, and everything it must leave alone ───────────


def test_an_identical_pacnew_is_deleted_and_named(etc) -> None:
    gone = pair(etc, "pacman.conf", b"same\n", b"same\n")
    (etc / "sub").mkdir(mode=0o755)
    deeper = pair(etc / "sub", "a.conf", b"x", b"x")
    assert pacnew_prune.prune(etc, owner=ME) == [str(gone), str(deeper)]
    assert not gone.exists() and not deeper.exists()
    assert (etc / "pacman.conf").read_bytes() == b"same\n", "the live file stays"


@pytest.mark.parametrize(
    ("live", "new"),
    [
        (b"mine\n", b"theirs\n"),  # different: a merge for a person
        (None, b"theirs\n"),  # original missing: a person looks first
        (b"same\n", b"same\n\n"),  # one byte more
        (b"", b"x"),
    ],
)
def test_a_pacnew_that_is_not_identical_is_left(etc, live, new) -> None:
    kept = pair(etc, "x.conf", live, new)
    assert pacnew_prune.prune(etc, owner=ME) == [] and kept.read_bytes() == new


@pytest.mark.parametrize("name", ["x.conf.pacsave", "x.conf.pacnew.bak", "x.conf.pacorig", "x.conf"])
def test_only_a_name_ending_in_pacnew_is_ever_deleted(etc, name) -> None:
    (etc / "x.conf.pacnew").write_bytes(b"other")  # keeps the live name of each candidate present and different
    (etc / name.removesuffix(".bak").removesuffix(".pacsave").removesuffix(".pacorig")).write_bytes(b"same")
    (etc / name).write_bytes(b"same")
    before = sorted(path.name for path in etc.iterdir())
    pacnew_prune.prune(etc, owner=ME)
    assert sorted(path.name for path in etc.iterdir()) == before


def test_a_bare_pacnew_name_is_not_a_pacnew(etc) -> None:
    (etc / ".pacnew").write_bytes(b"")
    assert pacnew_prune.prune(etc, owner=ME) == [] and (etc / ".pacnew").exists()


def test_nothing_outside_etc_is_touched_not_even_through_a_link(tmp_path, etc) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir(mode=0o755)
    far = pair(outside, "rc", b"same", b"same")
    (etc / "linked").symlink_to(outside, target_is_directory=True)  # a folder link inside /etc pointing away
    assert pacnew_prune.prune(etc, owner=ME) == []
    assert far.exists(), "a link to a folder is never entered"


def test_each_of_the_two_link_guards_holds_by_itself(tmp_path, etc, monkeypatch) -> None:
    """The walk does not enter a linked folder, and a file reached through one is refused anyway."""
    outside = tmp_path / "elsewhere"
    outside.mkdir(mode=0o755)
    far = pair(outside, "rc", b"same", b"same")
    (etc / "linked").symlink_to(outside, target_is_directory=True)
    looked: list[Path] = []
    real = pacnew_prune.identical
    monkeypatch.setattr(pacnew_prune, "identical", lambda path, owner=0: looked.append(path) or real(path, owner))
    pacnew_prune.prune(etc, owner=ME)
    assert looked == [], "nothing under the link was even looked at"
    assert real(etc / "linked" / far.name, owner=ME) is False, "a folder reached through a link is not a safe folder"


@pytest.mark.parametrize("linked", ["pacnew", "live"])
def test_a_link_is_never_compared_or_deleted(tmp_path, etc, linked) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"same")
    (etc / "x.conf").write_bytes(b"same")
    if linked == "pacnew":
        (etc / "x.conf.pacnew").symlink_to(target)
    else:
        (etc / "x.conf").unlink()
        (etc / "x.conf").symlink_to(target)
        (etc / "x.conf.pacnew").write_bytes(b"same")
    assert pacnew_prune.prune(etc, owner=ME) == []
    assert (etc / "x.conf.pacnew").is_symlink() or (etc / "x.conf.pacnew").exists()
    assert target.read_bytes() == b"same"


def test_a_folder_another_account_may_write_is_left_alone(etc) -> None:
    (etc / "open").mkdir()
    (etc / "open").chmod(0o777)
    kept = pair(etc / "open", "x.conf", b"same", b"same")
    assert pacnew_prune.prune(etc, owner=ME) == [] and kept.exists()


def test_a_folder_owned_by_another_account_is_left_alone(etc) -> None:
    kept = pair(etc, "x.conf", b"same", b"same")
    assert pacnew_prune.prune(etc, owner=ME + 1) == [] and kept.exists()


def test_the_shim_takes_no_arguments_and_runs_only_as_uid_0(capsys) -> None:
    assert pacnew_prune.main(["shim", "/home"]) == 2
    if os.geteuid() != 0:
        assert pacnew_prune.main(["shim"]) == 2
    assert pacnew_prune.ETC == Path("/etc") and pacnew.SHIM == Path("/usr/local/libexec/arch-update-pacnew-prune")


def test_the_shim_stands_alone_with_the_standard_library() -> None:
    """It runs as uid 0 from /usr/local/libexec, with python -I: nothing of this package is on its path."""
    source = Path(pacnew_prune.__file__).read_text()
    assert source.startswith("#!/usr/bin/python3 -I\n") and "from ." not in source and "arch_updater" not in source


# ── the account's side: a state for each ────────────────────────────────────


def test_each_pacnew_gets_its_state(etc) -> None:
    found = pacnew.states([
        str(pair(etc, "same.conf", b"a", b"a")),
        str(pair(etc, "other.conf", b"a", b"b")),
        str(pair(etc, "gone.conf", None, b"a")),
        str(etc / "old.conf.pacsave"),
    ])
    assert list(found.values()) == ["identical", "different", "original_missing", "pacsave"]


@pytest.mark.skipif(os.geteuid() == 0, reason="uid 0 reads every file")
def test_a_pacnew_this_account_cannot_read_is_unreadable_not_different(etc) -> None:
    path = pair(etc, "shadow", b"a", b"a")
    (etc / "shadow").chmod(0)
    assert pacnew.state(str(path)) == "unreadable"


# ── in the run ────────────────────────────────────────────────────────────────


@pytest.fixture
def shimmed(box, etc, tmp_path, monkeypatch):  # noqa: F811
    """The shim installed as this release ships it, and the run's sudo running it on the test's /etc."""
    shim = tmp_path / "libexec" / "arch-update-pacnew-prune"
    shim.parent.mkdir()
    shim.write_bytes(Path(pacnew_prune.__file__).read_bytes())
    monkeypatch.setattr(pacnew, "SHIM", shim)
    monkeypatch.setattr(pacnew_prune, "ETC", etc)
    box.sh.tools.add(shim.name)
    plain = box.sh.logged

    def logged(argv, log, timeout=0):
        if argv == ["sudo", "-n", str(shim)]:
            box.sh.ran.append(list(argv))
            return ok("".join(f"removed {path}\n" for path in pacnew_prune.prune(etc, owner=ME)))
        return plain(argv, log)

    monkeypatch.setattr(box.sh, "logged", logged)
    return box


def listed(box, paths):
    from arch_updater import watch

    box.patch.setattr(watch, "snapshot", lambda **kwargs: {"pacnew": [str(path) for path in paths]})


def test_a_run_deletes_identical_pacnew_through_the_shim_and_records_it(shimmed, etc) -> None:
    same, other, gone = pair(etc, "a.conf", b"a", b"a"), pair(etc, "b.conf", b"a", b"b"), pair(etc, "c.conf", None, b"c")
    listed(shimmed, [same, other, gone])
    report = shimmed.run()
    assert report["pacnew_removed"] == [str(same)] and not same.exists()
    assert report["pacnew"] == [str(other), str(gone)] and other.exists() and gone.exists()
    assert report["pacnew_states"] == {str(other): "different", str(gone): "original_missing"}
    assert report["pacnew_not_pruned"] == ""
    assert ["sudo", "-n", str(pacnew.SHIM)] in shimmed.sh.ran
    assert "removed 1 .pacnew identical to the live file" in shimmed.sh.calls[-1][-1]
    text = "\n".join(day.summary(report)[1])
    assert "removed, identical to the live file: " in text and "new config version with no live file: " in text and "compare: sudo pacdiff" in text
    status = auto.status()
    assert f"pacnew removed (identical to the live file): {same}" in status
    assert f"pacnew with no live file beside it, look before anything: {gone}\n    fix: sudo pacdiff" in status


def test_nothing_identical_runs_no_shim(shimmed, etc) -> None:
    listed(shimmed, [pair(etc, "b.conf", b"a", b"b")])
    report = shimmed.run()
    assert ["sudo", "-n", str(pacnew.SHIM)] not in shimmed.sh.ran and report["pacnew_removed"] == []


def test_a_dry_run_reads_the_states_and_deletes_nothing(shimmed, etc) -> None:
    same = pair(etc, "a.conf", b"a", b"a")
    listed(shimmed, [same])
    report = shimmed.run(dry_run=True)
    assert same.exists() and report["pacnew_states"] == {str(same): "identical"} and report["pacnew_removed"] == []
    assert ["sudo", "-n", str(pacnew.SHIM)] not in shimmed.sh.ran


def test_without_the_shim_an_identical_pacnew_is_listed_with_the_command_to_remove_it(shimmed, etc) -> None:
    shimmed.sh.tools.discard(pacnew.SHIM.name)
    same = pair(etc, "a.conf", b"a", b"a")
    listed(shimmed, [same])
    report = shimmed.run()
    assert same.exists() and report["pacnew_removed"] == [] and "is not installed" in report["pacnew_not_pruned"]
    assert f"fix: sudo rm {same}" in auto.status()
    assert f"remove: sudo rm {same}" in "\n".join(day.summary(report)[1])


def test_a_shim_older_than_this_release_is_not_run(shimmed, etc) -> None:
    pacnew.SHIM.write_text("#!/bin/sh\nrm -rf /etc\n")
    same = pair(etc, "a.conf", b"a", b"a")
    listed(shimmed, [same])
    report = shimmed.run()
    assert same.exists() and "older than this release" in report["pacnew_not_pruned"] and "sudo install" in report["pacnew_not_pruned"]
    assert ["sudo", "-n", str(pacnew.SHIM)] not in shimmed.sh.ran


def test_a_removal_line_outside_etc_is_not_believed(shimmed, etc, monkeypatch) -> None:
    same = pair(etc, "a.conf", b"a", b"a")
    listed(shimmed, [same])
    plain = shimmed.sh.logged
    monkeypatch.setattr(shimmed.sh, "logged", lambda argv, log, timeout=0: ok("removed /home/x.pacnew\nremoved " + str(same) + ".txt\n") if argv[-1] == str(pacnew.SHIM) else plain(argv, log))
    report = shimmed.run()
    assert report["pacnew_removed"] == [] and json.loads(json.dumps(report))["pacnew"] == [str(same)]


def test_the_sudoers_rule_for_the_shim_takes_no_arguments(shimmed) -> None:
    rules = [line for line in auto.sudoers_rules("u", sh=shimmed.sh).splitlines() if "pacnew" in line]
    assert rules == ['u ALL=(root) NOPASSWD: /usr/bin/arch-update-pacnew-prune ""']
