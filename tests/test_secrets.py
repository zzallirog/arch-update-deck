"""Credentials in URLs never reach a report, a notice, a brief, a census case or a share."""

from __future__ import annotations

import json

import pytest

from arch_updater import census, share_report
from arch_updater.textsafe import secrets
from test_auto import box, failed  # noqa: F401 - the scripted machine, as a fixture

LEAK = "error: failed retrieving file 'core.db' from https://bob:hunter2@mirror.example/core/os/x86_64 : 401\nerror: failed to synchronize all databases (?token=s3cr3t)"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("https://bob:hunter2@mirror.example/core", "https://<redacted>@mirror.example/core"),
        ("git clone https://ghp_abc123@github.com/x/y.git", "git clone https://<redacted>@github.com/x/y.git"),
        ("ftp://u:p@h/x", "ftp://<redacted>@h/x"),
        ("https://e.org/a?token=abc&x=1", "https://e.org/a?token=<redacted>&x=1"),
        ("https://e.org/a?x=1&access_token=abc#top", "https://e.org/a?x=1&access_token=<redacted>#top"),
        ("https://s3.example/o?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA/1", "https://s3.example/o?X-Amz-Signature=<redacted>&X-Amz-Credential=<redacted>"),
        ("https://e.org/f?sig=zz", "https://e.org/f?sig=<redacted>"),
    ],
)
def test_a_credential_in_a_url_is_hidden_and_the_rest_kept(text, kept) -> None:
    assert secrets(text) == kept


@pytest.mark.parametrize(
    "text",
    [
        "https://mirror.example/core/os/x86_64/core.db",
        "git@github.com:x/y.git",  # an ssh address names an account, not a secret
        "mail bob@example.org",
        "https://e.org/?keyboard=us&monkey=1&passage=2",
        "error: target not found: foo",
    ],
)
def test_text_without_a_credential_is_left_as_it_is(text) -> None:
    assert secrets(text) == text


def test_a_failing_store_leaves_no_credential_in_the_report_brief_or_case(box, tmp_path) -> None:  # noqa: F811
    box.sh.fail["repo"] = [failed(LEAK)]
    report = box.run()
    (item,) = report["trouble"]
    assert "hunter2" not in json.dumps(report) and "s3cr3t" not in json.dumps(report)
    assert "https://<redacted>@mirror.example" in item["detail"]
    assert all("hunter2" not in str(word) and "s3cr3t" not in str(word) for call in box.sh.calls for word in call), "nor the notice"
    # Everything the run wrote but its raw log: report, last-auto, census, brief (failure.log, brief.md, meta), events.
    written = {path.name: path.read_text() for path in tmp_path.rglob("*") if path.is_file() and path.name != "run.log"}
    assert {"report.json", "census.json", "failure.log", "brief.md"} <= set(written)
    assert [name for name, text in written.items() if "hunter2" in text or "s3cr3t" in text] == []


def test_a_case_recorded_without_the_secret_still_knows_the_failure_with_another_one(tmp_path) -> None:
    cases: list = []
    first = census.record(cases, tmp_path, "error: failed retrieving https://bob:hunter2@m.example/x", "repo", tmp_path / "run.log")
    assert "hunter2" not in first.match and "hunter2" not in first.meaning
    assert census.recognise(cases, "error: failed retrieving https://alice:other@m.example/x") is first


def test_a_share_carries_no_credential(tmp_path) -> None:
    case = census.Case(id="LOCAL-1", match="x", meaning="seen at https://e.org/a?token=abc", fix="curl https://u:p@e.org")
    shared = json.dumps(share_report.share_census([case], "/home/someone", "someone"))
    assert "abc" not in shared and "u:p@" not in shared and "<redacted>" in shared


def test_a_brief_written_from_raw_output_hides_the_credential_itself(tmp_path) -> None:
    """dotfiles and `learn` hand output to briefs.write without the run's own scrubbing in between."""
    from arch_updater import briefs

    folder = briefs.write("repo", census.Case(id="LOCAL-x"), LEAK, "auto-1", None, "", None, None, tmp_path)
    for name in ("failure.log", "brief.md"):
        text = (folder / name).read_text()
        assert "hunter2" not in text and "s3cr3t" not in text, name
