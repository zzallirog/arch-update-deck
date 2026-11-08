"""What the census collected reaches a person (C10), leaves clean (C11), arrives in a form (C13) and is counted (C14)."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from arch_updater import census, day, main, share_report
from arch_updater.census import Case
from test_auto import box  # noqa: F401 - box is a fixture
from test_day import waiting  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
WITNESSED = Case(id="LOCAL-WITNESSED-aabbccdd", match="are in conflict", fix="sudo pacman -R bar", store="repo", meaning="repo: somebody ran it")
UNKNOWN = Case(id="LOCAL-0f0f0f0f", match="flux", fix="arch-update brief", meaning="first seen 2026-11-01 in aur: flux")


# ── C10: the offer reaches a person ─────────────────────────────────────────────────────────────


def test_a_case_not_handed_upstream_puts_a_share_line_in_the_update_day_frame(box) -> None:  # noqa: F811
    waiting(box)
    census.save_local([WITNESSED, UNKNOWN], day.STATE_ROOT)
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("n\n"), out) == 0
    frame = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "share: 1 fix and 1 new failure from this machine not passed on yet" in frame and "arch-update census --share" in frame


def test_the_line_stays_until_the_cases_are_sent_and_is_gone_after(box) -> None:  # noqa: F811
    census.save_local([WITNESSED], day.STATE_ROOT)
    for _ in range(2):  # shown to the person who missed it, every visit
        out = io.StringIO()
        waiting(box)
        day.ask(box.sh, io.StringIO("n\n"), out)
        assert "share:" in out.getvalue()
    share_report.mark_sent([WITNESSED], day.STATE_ROOT)
    out = io.StringIO()
    waiting(box)
    day.ask(box.sh, io.StringIO("n\n"), out)
    assert "share:" not in out.getvalue()


def test_no_line_when_the_person_turned_sharing_off_or_there_is_nothing(tmp_path) -> None:
    assert share_report.share_line([WITNESSED], tmp_path, {"share": False}) == ""
    assert share_report.share_line([], tmp_path) == "" and share_report.share_line([census.Case(id="ROOT-SPACE-LOW")], tmp_path) == ""


def test_the_notification_offers_a_witnessed_fix_with_a_button(box) -> None:  # noqa: F811
    census.save_local([WITNESSED], box.root)
    box.sh.tools |= {"systemd-run", "kitty"}
    box.run()
    asked = box.sh.calls[-1]
    assert "--setenv=FIX=arch-update census --share" in asked and "--action=default=arch-update census --share" in asked
    assert any("Click to pass it on" in word for word in asked)
    box.run()
    assert not any("Click to pass" in word for word in box.sh.calls[-1]), "the notice offers once; the Update Day line is what stays"


# ── C11: nothing of this machine leaves with the census ─────────────────────────────────────────

LEAKS = {
    "the home folder": ("copied /home/alice/.cache/yay/x to /home/alice/y", "/home/alice"),
    "the login": ("build as alice failed; owner=alice", "alice"),
    "the host name": ("could not reach nas-box.lan from nas-box", "nas-box"),
    "a URL password": ("fetch https://bob:hunter2@mirror.example/x failed", "hunter2"),
    "a URL token": ("fetch https://mirror.example/x?token=s3cr3t&a=1 failed", "s3cr3t"),
    "a BSSID": ("wifi roaming to a4:5e:60:d1:22:9f failed", "a4:5e:60:d1:22:9f"),
    "a BSSID with dashes": ("ap A4-5E-60-D1-22-9F", "A4-5E-60-D1-22-9F"),
}


@pytest.mark.parametrize("leak", LEAKS)
def test_every_kind_of_leak_is_taken_out_of_every_field_that_is_shared(leak) -> None:
    text, secret = LEAKS[leak]
    case = Case(id="LOCAL-WITNESSED-1", match="x", meaning=text, fix="sudo true " + text, evidence=text, generic=text, source=text, store="repo", where=text)
    shared = share_report.share_census([case], "/home/alice", "alice", "nas-box.lan")
    flat = json.dumps(shared)
    assert secret.lower() not in flat.lower(), leak
    (item,) = shared["cases"]
    assert all(key in item for key in ("meaning", "fix", "evidence", "generic", "source", "where"))


def test_the_pieces_are_named_so_the_case_still_reads() -> None:
    case = Case(id="LOCAL-1", meaning="/home/alice/x as alice on nas-box via a4:5e:60:d1:22:9f")
    (item,) = share_report.share_census([case], "/home/alice", "alice", "nas-box")["cases"]
    assert item["meaning"] == "~/x as <user> on <host> via <bssid>"


def test_a_name_that_only_contains_the_login_or_host_stays() -> None:
    case = Case(id="LOCAL-1", meaning="malice and nas-boxes and 12:34 are fine, localhost too")
    (item,) = share_report.share_census([case], "/home/alice", "alice", "localhost")["cases"]
    assert item["meaning"] == case.meaning


def test_the_real_host_name_is_used_when_none_is_given(monkeypatch) -> None:
    monkeypatch.setattr(share_report.socket, "gethostname", lambda: "workstation7.lan")
    (item,) = share_report.share_census([Case(id="LOCAL-1", meaning="down: workstation7")], "", "")["cases"]
    assert item["meaning"] == "down: <host>"


# ── C13: the issue form ─────────────────────────────────────────────────────────────────────────


def form_ids() -> list[str]:
    return re.findall(r"^\s+id: (\w+)\s*$", (ROOT / ".github" / "ISSUE_TEMPLATE" / "census.yml").read_text(), re.M)


def test_the_issue_url_selects_the_census_form_and_fills_only_fields_it_has() -> None:
    shared = {"schema": "arch-update-census/v1", "cases": [{"id": "LOCAL-LEARNED-1", "fix": "true"}]}
    url = share_report.issue_url(shared)
    query = parse_qs(urlparse(url).query)
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / share_report.TEMPLATE).is_file() and query["template"] == ["census.yml"]
    assert query["labels"] == ["census"]
    assert set(query) - {"template", "labels", "title"} <= set(form_ids()), "every other key is a field of the form"
    assert json.loads(query["cases"][0]) == shared


def test_the_form_asks_for_the_cases_as_json_and_for_a_look_before_submit() -> None:
    text = (ROOT / ".github" / "ISSUE_TEMPLATE" / "census.yml").read_text()
    assert form_ids() == ["version", "cases", "consent"]
    assert "render: json" in text and 'labels: ["census"]' in text and "required: true" in text


# ── C14: the numbers ────────────────────────────────────────────────────────────────────────────


def state_with_history(root: Path) -> Path:
    def report(name: str, trouble: list[tuple[str, str]], fixed: int = 0, dry: bool = False) -> None:
        folder = root / "runs" / name
        folder.mkdir(parents=True)
        (folder / "report.json").write_text(json.dumps({"dry_run": dry, "trouble": [{"probe": p, "case": c, "status": "fail"} for p, c in trouble], "self_fixed": ["LOCAL-LEARNED-aa"] * fixed}))

    report("auto-1", [("repo", "PACMAN-REPLACEMENT-CONFLICT"), ("aur", "LOCAL-0f0f0f0f")])
    report("auto-2", [("repo", "LOCAL-LEARNED-aa")], fixed=1)
    report("auto-3", [("repo", "LOCAL-WITNESSED-aabbccdd")])
    report("auto-4", [("repo", "NO-SUCH")], dry=True)  # a dry run is not a failure of the week
    (root / "runs" / "auto-5").mkdir()  # no report: ignored
    census.save_local([WITNESSED, UNKNOWN, Case(id="LOCAL-LEARNED-aa", match="x", fix="true", repair="replay")], root)
    share_report.mark_sent([UNKNOWN], root)
    return root


def test_the_stats_count_failures_known_ones_self_fixed_witnessed_learned_and_shared(tmp_path) -> None:
    state = state_with_history(tmp_path)
    assert census.stats(state) == {"failures": 4, "recognised": 3, "fixed_by_itself": 1, "witnessed": 1, "learned": 1, "shared": 1}


def test_census_stats_prints_the_six_numbers_and_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    state = state_with_history(tmp_path)
    monkeypatch.setattr(main, "STATE_ROOT", state)
    before = (state / "census.json").read_bytes()
    assert main.main(["census", "--stats"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [int(line.split()[0]) for line in lines] == [4, 3, 1, 1, 1, 1]
    assert (state / "census.json").read_bytes() == before
    assert main.main(["census", "--stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["failures"] == 4


def test_counting_a_junk_census_does_not_rewrite_it(tmp_path) -> None:
    junk = (ROOT / "tests" / "fixtures" / "census" / "census-2026-11-01.json").read_text()
    (tmp_path / "census.json").write_text(junk)
    census.stats(tmp_path)
    assert (tmp_path / "census.json").read_text() == junk
