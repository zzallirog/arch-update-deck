import json
import time

import pytest

from arch_updater import stores
from arch_updater.engine import CommandResult


class Look:
    def __init__(self, answer):
        self.answer = answer

    def capture(self, argv, timeout=30):
        if argv == ["pacman", "-Qqm"]:
            return CommandResult(argv, 0, "app\nlib\n")
        if argv == ["pacman", "-Qm"]:
            return CommandResult(argv, 0, "app 1\nlib 1\n")
        return CommandResult(argv, 0, "1")

    def post(self, url, fields):
        return self.answer


def entry(name):
    return {"Name": name, "PackageBase": name, "Version": "2", "Maintainer": "alice", "LastModified": time.time() - 30 * 86400}


def test_an_answer_that_lists_fewer_than_it_counts_is_refused_not_read_as_nothing_fresh(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    state.write_text(json.dumps({"app": "alice", "lib": "alice"}))
    cut = {"type": "multiinfo", "version": 5, "resultcount": 2, "results": [entry("app")]}
    with pytest.raises(ValueError, match="incomplete"):
        stores.aur_review(Look(cut), 7, state)
    assert json.loads(state.read_text()) == {"app": "alice", "lib": "alice"}, "a refused answer must not touch the record"


def test_an_error_field_refuses_the_answer_even_with_the_right_type() -> None:
    with pytest.raises(ValueError, match="too many"):
        stores.aur_review(Look({"type": "multiinfo", "resultcount": 0, "results": [], "error": "too many"}), 7)


def test_a_whole_answer_passes(tmp_path) -> None:
    whole = {"type": "multiinfo", "version": 5, "resultcount": 2, "results": [entry("app"), entry("lib")]}
    assert stores.aur_review(Look(whole), 7, tmp_path / "m.json") == ([], [])
