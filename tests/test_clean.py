"""Text from packagers, builds and logs never reaches the terminal as commands."""

import json
import re

import pytest

from arch_updater import auto, day
from arch_updater.textsafe import clean
from test_auto import box, ok  # noqa: F401 - box is a fixture
from test_day import QUEUE, screen, verdicts, waiting  # noqa: F401

HOSTILE = [
    "\x1b]0;PWNED\x07",  # set the window title
    "\x1b[2J",  # clear the screen
    "\x9b2J",  # the same, in the 8-bit form
    "\x1b]52;c;cHduZWQ=\x07",  # write the clipboard
    "\x9d52;c;cHduZWQ=\x9c",  # the same, 8-bit
    "\x1b]8;;http://evil.example\x1b\\",  # a hyperlink
    "\x1bPqpwned\x1b\\",  # a device control string
    "\x1bc",  # reset the terminal
    "\x08\x07\x00\x7f",  # backspace, bell, NUL, DEL
]
# What the page itself writes: colour (SGR) and nothing else.
OWN = re.compile(r"\x1b\[(?:38;2;\d+;\d+;\d+(?:;48;2;\d+;\d+;\d+)?|0)m")


def unsafe(text: str) -> bool:
    return bool(re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]|PWNED|pwned|cHdu", OWN.sub("", text).replace("\n", "")))


@pytest.mark.parametrize("payload", HOSTILE)
def test_clean_removes_the_sequence_and_what_it_carried(payload) -> None:
    assert clean(f"before{payload}after") == "beforeafter"
    assert clean(f"a\tb\nc{payload}") == "a\tb\nc"


def test_clean_keeps_ordinary_text_even_when_it_is_odd() -> None:
    text = "kwin 6.7.5-1.1 → 6.7.5-3.1 · ünï ✓\n\tindented"
    assert clean(text) == text and clean("\x1b[31mred\x1b[0m") == "red"


@pytest.mark.parametrize("payload", HOSTILE)
def test_a_package_the_queue_names_cannot_write_to_the_terminal(box, payload) -> None:
    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok(f"evil{payload}pkg 1.0{payload} -> 2.0{payload}\n")]
    rows, _ = day.queue(box.sh)
    page = screen(rows=rows)
    assert not unsafe(page.frame()) and rows[0].name == "evilpkg"


@pytest.mark.parametrize("payload", HOSTILE)
def test_every_text_the_page_prints_is_clean(monkeypatch, payload) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)  # with colour on, only the page's own colour codes may be in the frame
    page = screen(rows=[day.Row(f"a{payload}b", f"1{payload}", f"2{payload}", "repo")], hanging=[f"root → fix{payload}"], bad=[("disk", f"low{payload}")])
    page.notes = [f"note{payload}"]
    page.event("look", {"verdicts": verdicts(("dns", f"no{payload}name"))})
    page.event("census", {"case": f"CASE{payload}", "attempted": False})
    page.event("keep", {"path": f"/keep{payload}"})
    page.log += ["", *day.summary({"crash": f"broke{payload}", "updated": {f"s{payload}": 1}, "trouble": [{"probe": f"p{payload}", "detail": f"d{payload}", "fix": f"f{payload}"}], "held": {"aur_too_fresh": [f"x{payload}"]}, "pacnew": [f"/etc/y{payload}"], "exit_code": 30}, [day.Row(f"n{payload}", "1", "2", "repo")])[1]]
    for frame in (page.frame(), screen(rows=page.rows, bad=[("disk", f"low{payload}")]).frame()):
        assert not unsafe(frame)


@pytest.mark.parametrize("payload", HOSTILE)
def test_the_log_on_the_l_key_is_clean(payload) -> None:
    run = day.STATE_ROOT / "runs" / "auto-1"
    run.mkdir(parents=True)
    (run / "run.log").write_text(f"line one\nbuilding{payload}now\n")
    (day.STATE_ROOT / "last-auto.json").write_text(json.dumps({"run_id": "auto-1"}))
    text = "\n".join(day.last_log(30))
    assert "buildingnow" in text and not unsafe(text)


@pytest.mark.parametrize("payload", HOSTILE)
def test_status_prints_what_the_report_says_without_its_escapes(box, payload) -> None:
    (box.root / "last-auto.json").write_text(
        json.dumps({"state": "RECOVERY_PENDING", "finished_at": "2026-10-08T04:09", "updated": {}, "crash": f"c{payload}", "held": {}, "pacnew": [f"/etc/x{payload}"], "reboot_required": False,
                    "trouble": [{"probe": "repo", "case": "X", "detail": f"d{payload}", "meaning": "", "fix": f"f{payload}"}]})
    )
    assert not unsafe(auto.status())
