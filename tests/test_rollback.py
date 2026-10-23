"""`arch-update rollback`: the record's versions against the machine now, the cache files, and a yes before anything."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from arch_updater import main, rollback
from arch_updater.shell import Shell

RECORD = "auto-20261020T120000+0300"


class Box(Shell):
    def __init__(self, now, cache):
        self.now, self.cache, self.typed = now, cache, []

    def capture(self, argv, timeout=30):
        if argv == ["pacman", "-Q"]:
            return SimpleNamespace(returncode=0, output="".join(f"{name} {version}\n" for name, version in self.now.items()))
        if argv == ["pacman-conf", "CacheDir"]:
            return SimpleNamespace(returncode=0, output=f"{self.cache}/\n")
        raise AssertionError(argv)

    def interactive(self, argv):
        self.typed.append(argv)
        return 0


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def ground(tmp_path):
    keep, cache = tmp_path / "keep", tmp_path / "pkg"
    (keep / RECORD).mkdir(parents=True)
    (keep / RECORD / "packages.txt").write_text("linux 6.17.9-1\nglibc 2.42-1\ngone 1-1\nsame 3-1\nlost 2-1\n")
    cache.mkdir()
    for name in ("linux-6.17.9-1-x86_64.pkg.tar.zst", "glibc-2.42-1-x86_64_v4.pkg.tar.zst", "glibc-2.42-1-x86_64_v4.pkg.tar.zst.sig", "gone-1-1-any.pkg.tar.zst",
                 "linux-6.18.1-1-x86_64.pkg.tar.zst", "lost-tools-2-1-x86_64.pkg.tar.zst"):
        (cache / name).write_bytes(b"")
    now = {"linux": "6.18.1-1", "glibc": "2.42-2", "same": "3-1", "lost": "2-2", "fresh": "1-1"}
    return SimpleNamespace(keep=keep, cache=cache, sh=Box(now, cache))


def test_the_plan_finds_exactly_the_old_versions_in_the_cache(ground) -> None:
    found = rollback.plan(ground.sh, ground.keep / RECORD)
    steps = {step.name: (step.now, step.was, step.file) for step in found.steps}
    assert steps == {
        "linux": ("6.18.1-1", "6.17.9-1", str(ground.cache / "linux-6.17.9-1-x86_64.pkg.tar.zst")),
        "glibc": ("2.42-2", "2.42-1", str(ground.cache / "glibc-2.42-1-x86_64_v4.pkg.tar.zst")),  # not the .sig beside it
        "gone": (None, "1-1", str(ground.cache / "gone-1-1-any.pkg.tar.zst")),  # removed since: put back too
        "lost": ("2-2", "2-1", None),  # lost-tools-2-1 has a name that only starts like it: not taken
    }
    assert found.added == ["fresh"]


def test_an_aur_package_is_found_in_yays_folder(ground, tmp_path, monkeypatch) -> None:
    yay = tmp_path / "home" / ".cache" / "yay" / "lost"
    yay.mkdir(parents=True)
    (yay / "lost-2-1-x86_64.pkg.tar.zst").write_bytes(b"")
    found = rollback.plan(ground.sh, ground.keep / RECORD)
    assert next(step.file for step in found.steps if step.name == "lost") == str(yay / "lost-2-1-x86_64.pkg.tar.zst")


def test_the_plan_names_what_it_skips_and_what_it_does_not_undo(ground) -> None:
    text = rollback.describe(rollback.plan(ground.sh, ground.keep / RECORD))
    assert "lost" in text and "not in the cache: skipped" in text
    assert "installed since; not undone (by hand: sudo pacman -Rs fresh)" in text
    assert "1 cannot be put back from the cache" in text
    assert "to roll back: sudo pacman -U -- " in text and "--noconfirm" not in text


def test_without_a_terminal_it_only_prints(ground) -> None:
    out = io.StringIO()
    assert rollback.run(ground.sh, ground.keep, stdin=io.StringIO("y\n"), out=out) == 0
    assert ground.sh.typed == [] and "to roll back:" in out.getvalue()


def test_yes_without_a_terminal_installs_nothing_and_says_why(ground) -> None:
    out = io.StringIO()
    assert rollback.run(ground.sh, ground.keep, yes=True, stdin=io.StringIO(""), out=out) == 1
    assert ground.sh.typed == [] and "run this at a terminal" in out.getvalue()


@pytest.mark.parametrize("answer", ["\n", "n\n", "no\n", ""])
def test_at_a_terminal_anything_but_yes_changes_nothing(ground, answer) -> None:
    out = Tty()
    assert rollback.run(ground.sh, ground.keep, stdin=Tty(answer), out=out) == 0
    assert ground.sh.typed == [] and "Roll back 3 packages with pacman -U? [y/N]" in out.getvalue() and "nothing was changed" in out.getvalue()


def test_at_a_terminal_yes_runs_pacman_u_on_the_cached_files_and_pacman_still_asks(ground) -> None:
    assert rollback.run(ground.sh, ground.keep, stdin=Tty("y\n"), out=Tty()) == 0
    (argv,) = ground.sh.typed
    assert argv[:4] == ["sudo", "pacman", "-U", "--"] and "--noconfirm" not in argv
    assert sorted(argv[4:]) == sorted(str(ground.cache / name) for name in ("linux-6.17.9-1-x86_64.pkg.tar.zst", "glibc-2.42-1-x86_64_v4.pkg.tar.zst", "gone-1-1-any.pkg.tar.zst"))


def test_the_flag_yes_skips_only_the_tools_own_question(ground) -> None:
    out = Tty()
    assert rollback.run(ground.sh, ground.keep, yes=True, stdin=Tty(""), out=out) == 0
    assert len(ground.sh.typed) == 1 and "[y/N]" not in out.getvalue()


def test_the_newest_record_is_the_default_and_another_can_be_named(ground) -> None:
    older = ground.keep / "auto-20261001T120000+0300"
    older.mkdir()
    (older / "packages.txt").write_text("linux 6.18.1-1\nglibc 2.42-2\nsame 3-1\nlost 2-2\nfresh 1-1\n")
    out = io.StringIO()
    rollback.run(ground.sh, ground.keep, stdin=io.StringIO(""), out=out)
    assert out.getvalue().startswith(f"record {RECORD}:")
    out = io.StringIO()
    rollback.run(ground.sh, ground.keep, "auto-20261001T120000+0300", stdin=io.StringIO(""), out=out)
    assert out.getvalue() == "nothing changed since the record auto-20261001T120000+0300\n"
    with pytest.raises(ValueError, match="no record auto-1"):
        rollback.run(ground.sh, ground.keep, "auto-1", stdin=io.StringIO(""), out=io.StringIO())


def test_without_a_record_the_command_says_so_and_exits_1(tmp_path, capsys) -> None:
    assert main.main(["rollback"]) == 1
    assert "no record yet" in capsys.readouterr().err
