"""Commands whose output is read with English patterns run with LC_ALL=C; the owner's own commands keep their language."""

from __future__ import annotations

import os

import pytest

from arch_updater import census, engine
from arch_updater.shell import Shell


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Programs that print the language they were started in, and a sudo that runs what follows -n."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in ("pacman", "yay", "checkupdates", "my-backup"):
        (folder / name).write_text('#!/bin/sh\nprintf "%s" "${LC_ALL:-unset}"\n')
        (folder / name).chmod(0o755)
    (folder / "sudo").write_text('#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
    (folder / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    return folder


@pytest.mark.parametrize(
    ("argv", "language"),
    [
        (["pacman", "-Q"], "C"),
        (["checkupdates"], "C"),
        (["sudo", "-n", "pacman", "-Syu"], "C"),
        (["sudo", "-n", "yay", "-Sua"], "C"),
        (["my-backup"], "ru_RU.UTF-8"),  # a condition the owner wrote: their language, not ours
        (["sudo", "-n", "my-backup"], "ru_RU.UTF-8"),
    ],
)
def test_a_parsed_command_runs_in_c_and_any_other_in_the_owners_language(tools, tmp_path, argv, language) -> None:
    # sh may first warn that the made-up language is not installed: the last line is what the program printed
    assert engine.run_capture(argv).output.splitlines()[-1] == language
    logged = Shell().logged(argv, tmp_path / "run.log")
    assert logged.output.strip().splitlines()[-2] == language, "the line before the log's [exit 0]"


def test_a_failure_in_another_language_would_not_be_recognised_so_the_store_is_run_in_c(tmp_path) -> None:
    """Why it matters: the catalogue reads pacman's English words; the same failure in Russian is an unseen case."""
    cases = census.load(tmp_path)
    assert census.recognise(cases, "error: failed to synchronize: unable to lock database").id == "PACMAN-LOCK"
    assert census.recognise(cases, "\u043e\u0448\u0438\u0431\u043a\u0430: \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u0442\u044c: \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0431\u0430\u0437\u0443 \u0434\u0430\u043d\u043d\u044b\u0445") is None
    assert {"pacman", "yay", "paru", "checkupdates", "pacdiff"} <= engine.C_LOCALE
