"""The census forgets what is only this machine's (C6, C7) and names the tool's own refusals (C8)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from arch_updater import census
from test_auto import box, failed  # noqa: F401 - box is a fixture

FIXTURES = Path(__file__).parent / "fixtures" / "census"
LOG = Path("run.log")


def local(cases):
    return [case for case in cases if case.id.startswith("LOCAL-")]


# Two failures of one class, each with names of its own: a package file in a cache, a version, a hash, a URL, a path.
PAIRS = [
    ("-> error installing: [/home/a/.cache/yay/foo/foo-1.0-1-x86_64.pkg.tar.zst] - exit status 1",
     "-> error installing: [/home/b/.cache/yay/bar/bar-22.3.4-7-any.pkg.tar.zst /home/b/.cache/yay/baz/baz-9-1-x86_64.pkg.tar.zst] - exit status 1"),
    ("error: could not unpack foo-1.0-1 (flux capacitor)", "error: could not unpack quux-20.1.2-3 (flux capacitor)"),
    ("error: the checkout stopped at commit 4f508be1c2 of the flux capacitor", "error: the checkout stopped at commit 9a8b7c6d5e4f of the flux capacitor"),
    ("error: flux capacitor at https://one.example/x/y?t=1 is down", "error: flux capacitor at https://two.example/other/path is down"),
    ("error: flux capacitor cannot read /var/lib/one/db", "error: flux capacitor cannot read /srv/two/other/db"),
]


@pytest.mark.parametrize(("first", "second"), PAIRS)
def test_two_failures_of_one_class_with_other_packages_are_one_local_case_seen_twice(tmp_path, first, second) -> None:
    cases: list[census.Case] = []
    one = census.record(cases, tmp_path, first, "aur", LOG)
    two = census.record(cases, tmp_path, second, "aur", LOG)
    assert one is two and len(local(cases)) == 1 and one.seen == 2
    saved = json.loads((tmp_path / "census.json").read_text())
    assert [(item["id"], item["seen"]) for item in saved] == [(one.id, 2)]
    assert census.recognise(cases, second) is one, "and the next one is known"


@pytest.mark.parametrize(("first", "second"), PAIRS)
def test_a_pattern_finds_the_line_it_came_from(first, second) -> None:
    for line in (first, second):
        pattern, shown = census.pattern_for(line)
        assert re.search(pattern, line), shown
        assert census.normalize(shown) == shown, "naming it twice changes nothing"


def test_no_path_version_package_file_or_hash_is_left_in_a_local_case(tmp_path) -> None:
    line = "error: /home/zed/.cache/yay/foo/foo-1.2.3-4-x86_64.pkg.tar.zst bad, commit 4f508be1c2, https://u.example/p/q"
    case = census.record([], tmp_path, line, "aur", LOG)
    for leak in ("/home/zed", "foo-1.2.3", ".pkg.tar", "4f508be1c2", "u.example"):
        assert leak not in case.match and leak not in case.meaning, leak


def test_a_failure_with_numbers_only_changing_keeps_the_old_id(tmp_path) -> None:
    """The id of a plain failure is what it always was: files written before this rule are not renamed."""
    line = "error: the flux capacitor at 0x7f3a returned 88 gigawatts"
    old = census._VOLATILE.sub(lambda _: r"(?:0x[0-9a-fA-F]+|\d+)", re.escape(line))
    assert census.record([], tmp_path, line, "repo", LOG).match == old


# ── C7: what was recorded before is recomputed on load ──────────────────────────────────────────


def test_the_live_census_of_two_junk_cases_is_one_case_after_loading(tmp_path) -> None:
    shutil.copy(FIXTURES / "census-2026-11-01.json", tmp_path / "census.json")
    before = json.loads((tmp_path / "census.json").read_text())
    assert len(before) == 2 and all(item["seen"] == 1 for item in before), "the live file this fixture is a copy of"
    cases = local(census.load(tmp_path))
    assert len(cases) == 1 and cases[0].seen == 2
    assert "/home/" not in cases[0].match and ".pkg" not in cases[0].match
    assert len(json.loads((tmp_path / "census.json").read_text())) == 1, "kept, so it is done once"
    assert [(case.id, case.seen) for case in local(census.load(tmp_path))] == [(cases[0].id, 2)], "and loading again changes nothing"


def test_the_cleaned_case_still_finds_what_the_junk_ones_were_for(tmp_path) -> None:
    shutil.copy(FIXTURES / "census-2026-11-01.json", tmp_path / "census.json")
    cases = census.load(tmp_path)
    for meaning in (item["meaning"] for item in json.loads((FIXTURES / "census-2026-11-01.json").read_text())):
        line = meaning.split(": ", 1)[1]
        found = census.recognise(cases, line)
        assert found is not None and found.id.startswith("LOCAL-")


def test_loading_leaves_learned_witnessed_and_shipped_cases_alone(tmp_path) -> None:
    mine = [
        census.Case(id="LOCAL-LEARNED-aa", match="x (?:\\d+)", fix="true", repair="replay", meaning="repo: it was /home/zed/x", seen=3),
        census.Case(id="LOCAL-WITNESSED-bb", match="y", fix="sudo pacman -R y", repair="replay", meaning="seen 2026-10-25 in repo: y", seen=0),
    ]
    census.save_local(mine, tmp_path)
    loaded = {case.id: case for case in census.load(tmp_path)}
    assert loaded["LOCAL-LEARNED-aa"].match == "x (?:\\d+)" and loaded["LOCAL-LEARNED-aa"].seen == 3
    assert loaded["LOCAL-WITNESSED-bb"].repair is None, "whatever a file says, a witnessed fix is never replayed"
    assert census.find(census.load(tmp_path), "ROOT-SPACE-LOW").repair == "trim-cache"


def test_a_census_file_that_cannot_be_read_is_an_empty_one(tmp_path) -> None:
    (tmp_path / "census.json").write_text("{not json")
    assert local(census.load(tmp_path)) == []


# ── C8: the tool's own refusal is its own case ──────────────────────────────────────────────────


def test_the_live_refusal_of_the_eighth_of_november_is_tool_refused_and_not_a_build_failure() -> None:
    output = (FIXTURES / "failure-2026-11-08-aur-tail.log").read_text()
    assert "refused" in output and "error installing" in output
    cases = census.load(Path("/nonexistent"))
    assert census.recognise(cases, output, "aur").id == "TOOL-REFUSED"
    assert census.recognise(cases, output + "\nerror making: foo exit status 4\n", "aur").id == "TOOL-REFUSED", "whatever else it says"


def test_a_refusal_is_never_filed_as_a_new_local_failure(box) -> None:  # noqa: F811
    output = (FIXTURES / "failure-2026-11-08-aur-tail.log").read_text()
    box.sh.fail["aur"] = [failed(output)]
    report = box.run()
    assert [(item["probe"], item["case"]) for item in report["trouble"]] == [("aur", "TOOL-REFUSED")]
    assert "refused" in report["trouble"][0]["meaning"] or "on purpose" in report["trouble"][0]["meaning"]
    assert not (box.root / "census.json").exists() or local(census.load(box.root)) == []


def test_a_line_of_the_catalogue_names_it() -> None:
    entry = next(case for case in census.load(Path("/nonexistent")) if case.id == "TOOL-REFUSED")
    assert entry.matches("arch-update: an unattended AUR update only installs packages; refused: pacman -U x")
    assert entry.matches("arch-update: refused, nothing installed:\nreason")
    assert not entry.matches("the maintainer refused my patch")
