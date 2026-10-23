"""pacman.conf's IgnorePkg makes an update partial: the run names what stayed behind; HoldPkg has its own case."""

from __future__ import annotations

import pytest

from arch_updater import auto, census, day, stores
from test_auto import box, failed, ok  # noqa: F401 - the scripted machine, as a fixture

QU = "linux 6.17.9-1 -> 6.18.1-1\nhunspell-uk 1.0-1 -> 1.1-1 [ignored]\nmesa 1-1 -> 2-1 [ignored]\n"


class Qu:
    def __init__(self, returncode, output):
        self.answer = (returncode, output)
        self.asked = []

    def capture(self, argv, timeout=30):
        self.asked.append(argv)
        return failed(self.answer[1], self.answer[0]) if self.answer[0] else ok(self.answer[1])


@pytest.mark.parametrize(
    ("returncode", "output", "held"),
    [(0, QU, ["hunspell-uk 1.0-1 -> 1.1-1", "mesa 1-1 -> 2-1"]), (1, "", []), (0, "linux 1 -> 2\n", []), (127, QU, [])],
)
def test_only_what_pacman_marks_ignored_is_named(returncode, output, held) -> None:
    sh = Qu(returncode, output)
    assert stores.ignored(sh) == held and sh.asked == [["pacman", "-Qu"]]


def test_a_run_names_the_packages_ignorepkg_held_back(box) -> None:  # noqa: F811
    box.sh.fail["pacman -Qu"] = [ok(QU)]
    report = box.run()
    assert report["held"] == {"ignored": ["hunspell-uk 1.0-1 -> 1.1-1", "mesa 1-1 -> 2-1"]}
    assert report["exit_code"] == 0, "a partial update is said, not failed"
    assert "held: ignored" in box.sh.calls[-1][-1]
    assert "held ignored: hunspell-uk 1.0-1 -> 1.1-1, mesa 1-1 -> 2-1" in auto.status()
    text = "\n".join(day.summary(report)[1])
    assert "held by IgnorePkg or IgnoreGroup in pacman.conf, a partial update: " in text and "mesa 1-1 -> 2-1" in text


def test_nothing_is_read_when_the_repository_update_failed(box) -> None:  # noqa: F811
    box.sh.fail["repo"] = [failed("error: failed to synchronize all databases")]
    box.sh.fail["pacman -Qu"] = [ok(QU)]
    report = box.run()
    assert "ignored" not in report["held"] and ["pacman", "-Qu"] not in box.sh.calls


def test_a_holdpkg_in_the_way_is_its_own_case(tmp_path) -> None:
    output = "warning: glibc is designated as a HoldPkg.\n:: HoldPkg was found in target list. Do you want to continue? [y/N]\nerror: failed to prepare transaction"
    case = census.recognise(census.load(tmp_path), output)
    assert case.id == "HOLDPKG-IN-TARGETS" and not case.repair and "HoldPkg" in case.fix
