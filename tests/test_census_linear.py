"""A census pattern is matched in linear time, per line, with a clock (builder pass 2, ReDoS)."""

from __future__ import annotations

import re
import time

import pytest

from arch_updater import census, witness
from test_auto import box, failed  # noqa: F401 - box is a fixture

COPY = "error: failed to copy /home/u/a to /home/u/b: No such file or directory"
LOOKALIKE = "error: failed to copy /home/u/a to /home/u/b: Permission denied"
OLD_HOLE = r"error:\ failed\ to\ copy\ \S+(?:\s+\S+)*?\ to\ \S+(?:\s+\S+)*?:\ No\ such"  # what the first pass wrote
OLD_GROUP = r"\S+\s+and\s+(?P<pkg>[A-Za-z0-9@_+][A-Za-z0-9@._+-]*?)-\d[\w.+:~]*-\d+(?:\.\d+)?\s+are\s+in\s+conflict"


def took(call) -> float:
    """The seconds of CPU the call burned: a throttled or busy machine makes the wall clock lie; the wall clock is bounded too."""
    wall, cpu = time.perf_counter(), time.process_time()
    call()
    assert time.perf_counter() - wall < 20, "the wall clock, much looser than the CPU bound that follows"
    return time.process_time() - cpu


def test_a_hole_pattern_on_many_lookalike_lines_is_fast() -> None:
    case = census.Case(id="LOCAL-1", match=census.pattern_for(COPY)[0])
    assert took(lambda: case.matches("\n".join([LOOKALIKE] * 1000))) < 0.5, "it was 16 s with the unbounded hole"
    assert case.matches(COPY) and case.matches("x\n" + COPY.replace("/home/u/a", "/srv/q w/z"))


def test_the_pattern_the_first_pass_wrote_is_not_used_at_all_and_costs_nothing() -> None:
    case = census.Case(id="LOCAL-2", match=OLD_HOLE)
    assert took(lambda: case.matches("\n".join([LOOKALIKE] * 80))) < 0.5 and not case.matches(COPY)


def test_the_witnessed_group_on_a_16_kb_token_is_fast() -> None:
    old = census.Case(id="LOCAL-WITNESSED-1", match=OLD_GROUP)
    for token in ("x and " + "a-" * 8000 + "9-9 zzz", "x and a" + "-1" * 8000):
        assert took(lambda: old.matches(token)) < 0.5, "the old group took 8 s"
    new = witness.generalize(":: foo-1-1 and bar-2-1 are in conflict. Remove bar? [y/N]\n", ["bar"])
    case = census.Case(id="LOCAL-WITNESSED-2", match=new.match, fix="sudo pacman -R bar", generic="sudo pacman -R {pkg}")
    for token in ("x and " + "a-" * 8000 + "9-9 zzz", "x and a" + "-1" * 8000, "a" * 16384, "a-1 " * 4000):
        assert took(lambda: case.matches(token)) < 0.5 and took(lambda: case.fix_for(token)) < 0.5


@pytest.mark.parametrize("pattern", [r"(\w+\s?)*;;EVIL", r"(a+)+$", r"(a|aa)+$", r"([a-z]+)*\d", r"(\S+\s*)+x"])
def test_a_pattern_with_a_repeat_in_a_repeat_is_refused(pattern) -> None:
    assert not census.usable(pattern)
    assert took(lambda: census.Case(id="LOCAL-3", match=pattern).matches("a" * 40)) < 0.5


@pytest.mark.parametrize("pattern", [census.pattern_for(COPY)[0], r"error: (?:0x[0-9a-f]+|\d+) gigawatts", r"failed retrieving file", r"\A\s*\Z", "(?i)x(?:a|b){1,10}y"])
def test_the_patterns_the_tool_writes_are_usable(pattern) -> None:
    assert census.usable(pattern)


def test_a_pattern_that_does_not_compile_is_not_a_match() -> None:
    assert not census.usable("(unclosed") and not census.Case(id="LOCAL-4", match="(unclosed").matches("(unclosed")


def test_a_pattern_never_spans_a_newline() -> None:
    assert census.Case(id="LOCAL-5", match=r"first\s+second").matches("first second")
    assert not census.Case(id="LOCAL-5", match=r"first\s+second").matches("first\nsecond")
    assert not census.Case(id="LOCAL-5", match=r"(?s)first.*second").matches("first\nsecond")


def test_only_the_tail_of_a_long_log_and_the_start_of_a_long_line_is_read() -> None:
    needle = census.Case(id="LOCAL-6", match="needle in the haystack")
    assert not needle.matches("needle in the haystack\n" + "x\n" * census.MATCH_CHARS)
    assert needle.matches("x\n" * census.MATCH_CHARS + "needle in the haystack")
    assert needle.matches("x\n" * 1000 + "needle in the haystack " + "x" * 3000)
    assert not needle.matches("x" * (census.LINE_CHARS + 1) + "needle in the haystack")


def test_a_shipped_pattern_is_not_limited() -> None:
    shipped = census.Case(id="ROOT-SPACE-LOW", match=r"(?s)first.*second")
    assert shipped.matches("first\nsecond")


def test_a_match_that_still_runs_long_is_stopped_by_the_clock(monkeypatch) -> None:
    monkeypatch.setattr(census, "usable", lambda pattern: True)  # as if the static and trial checks had missed it
    monkeypatch.setattr(census, "MATCH_SECONDS", 0.2)
    case = census.Case(id="LOCAL-7", match=r"(a+)+$")
    assert not case.matches("a" * 60 + "!")
    assert took(lambda: case.matches("a" * 60 + "!")) < 0.5, "an unstopped match runs for minutes"


def test_a_failed_update_with_many_lookalike_lines_does_not_stall_the_run(box) -> None:  # noqa: F811
    box.sh.fail["repo"] = [failed(COPY)]
    case = box.run()["trouble"][0]["case"]
    assert case.startswith("LOCAL-")
    box.sh.fail["repo"] = [failed("\n".join([LOOKALIKE] * 250))]
    assert took(box.run) < 5, "the same run took 17 s on 120 lines and grew with the cube"
