"""The one screen: what it shows, what a key does, what the timer starts.

Frames are compared whole against tests/fixtures/*.txt, in plain text. After a deliberate change of the
design, `UPDATE_FIXTURES=1 pytest tests/test_day.py` rewrites them; read the diff before committing it.
"""

import contextlib
import fcntl
import io
import json
import os
import pty
import re
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arch_updater import auto, census, day, exits, main as cli, watch
from arch_updater.keepalive import Keepalive
from arch_updater.shell import Shell
from test_auto import CONFLICT, UPGRADE, box, failed, ok  # noqa: F401 - box is a fixture

FIXTURES = Path(__file__).parent / "fixtures"
QUEUE = "kwin 6.7.5-1.1 -> 6.7.5-3.1\nqt6-svg 6.11.2-1.1 -> 6.12.0-1.1\n"
# The probes' own list; "conditions" answers once per configured guard, so with none configured it says nothing.
CHECKS = tuple(probe.__name__.removeprefix("probe_") for probe in watch.PROBES if probe is not watch.probe_conditions)
JARGON = ("@CTL", "@FIX", "ACC", "WATCH", "CENSUS", "MOV ", "PORTS")
ZEN, DISCORD = day.Row("zen-browser-bin", "1.23b-1", "1.23.1b-1", "repo"), day.Row("discord-canary", "1.0.2139-1", "1.0.2140-1", "aur")


@pytest.fixture(autouse=True)
def plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(day, "now", lambda: datetime(2026, 10, 8, 4, 9, tzinfo=timezone.utc))


class Terminal(io.StringIO):
    def isatty(self):
        return True


class Silent(io.StringIO):
    """Standard input that must never be read: look-only and nothing-to-ask visits take no key."""

    def readline(self, *args):
        raise AssertionError("a key was read")


@contextlib.contextmanager
def keyboard(keys: bytes, out: io.StringIO):
    """A real terminal for the keys (a pty), so the screen believes someone is sitting there.

    The keys are typed once the question is on screen: switching a terminal to key-at-a-time mode drops what was typed before.
    """
    master, slave = pty.openpty()

    def type_keys() -> None:
        deadline = time.monotonic() + 10
        while "UPDATE NOW?" not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        os.write(master, keys)

    typist = threading.Thread(target=type_keys)
    typist.start()
    stream = os.fdopen(slave, "r")
    try:
        yield stream
    finally:
        typist.join()
        stream.close()
        os.close(master)


def verdicts(*bad):
    return [{"probe": name, "status": "pass", "detail": ""} for name in CHECKS if name not in dict(bad)] + [
        {"probe": name, "status": "blocked", "detail": detail, "case": name.upper()} for name, detail in bad
    ]


def screen(columns=100, lines=30, rows=(), unread=(), bad=(), hanging=(), asking=True):
    page = day.Screen((columns, lines))
    page.rows, page.unread, page.verdicts, page.hanging = list(rows), list(unread), verdicts(*bad), list(hanging)
    if asking:
        page.prompt, page.keys = "UPDATE NOW?  [y] yes   [N] not now   Enter means no", True
    return page


def finished(report, rows=(), changes=()):
    page = screen(rows=rows, asking=False)
    page.event("look", {"verdicts": verdicts()})
    page.event("store", {"name": "repo", "phase": "start"})
    page.event("store", {"name": "repo", "phase": "done", "changed": len(changes), "ok": report["exit_code"] != 30})
    headline, lines = day.summary({"crash": None, "trouble": [], "held": {}, "pacnew": [], "reboot_required": False, **report}, list(changes))
    page.log += ["", *lines]
    page.prompt = f"{headline}   [Enter] close"
    return page


def unanswered(page, prompt):
    """The screen as it ends when there is nothing to ask: the line that says why, and no keys."""
    page.prompt, page.keys = prompt, False
    return page


def states():
    many = [day.Row(f"package-{n:02d}", "1.2.3-4", "1.2.4-1", "repo") for n in range(1, 61)]
    long = [day.Row("a-package-with-a-really-long-name-indeed", "20260101.123456.abcdef-1", "20260301.654321.fedcba-1", "repo"), DISCORD]
    nothing = screen(asking=False)
    nothing.rows = []
    nothing.notes = [day.WEEKLY_OFF]
    changes = [day.Row("kwin", "6.7.5-1.1", "6.7.5-3.1", "repo"), day.Row("qt6-svg", "6.11.2-1.1", "6.12.0-1.1", "repo")]
    running = screen(rows=[ZEN], asking=False)
    running.event("look", {"verdicts": verdicts()})
    running.event("store", {"name": "repo", "phase": "start"})
    running.prompt = "running …"
    return {
        "nothing_waiting": nothing,
        "two_packages": screen(rows=[ZEN, DISCORD]),
        "unread_queue": unanswered(screen(unread=["repo"], asking=False), day.unread_prompt(False)),
        "only_aur_by_hand": unanswered(screen(rows=[DISCORD], asking=False), day.BY_HAND_ONLY),
        "problems_pending": screen(rows=[ZEN], bad=[("disk", "5.1 GiB free, 6 GiB needed")], hanging=["root access → arch-update auto --sudoers", "repo → sudo pacman -Syu"]),
        "running": running,
        "done_reboot": finished({"updated": {"repo": 2}, "exit_code": 10, "reboot_required": True}, changes=changes),
        "failed_with_fix": finished(
            {"updated": {}, "exit_code": 30, "trouble": [{"probe": "repo", "detail": "error: unresolvable package conflicts detected", "fix": census.find(census.load(day.STATE_ROOT), "PACMAN-REPLACEMENT-CONFLICT").fix}]}
        ),
        "narrow_80": screen(80, 24, rows=long),
        "overflow_40_rows": screen(100, 40, rows=many),
    }


@pytest.mark.parametrize("name", list(states()))
def test_the_frame_is_exactly_the_fixture(name) -> None:
    frame = states()[name].frame()
    path = FIXTURES / f"{name}.txt"
    if os.environ.get("UPDATE_FIXTURES"):
        FIXTURES.mkdir(exist_ok=True)
        path.write_text(frame, encoding="utf-8")
    assert frame == path.read_text(encoding="utf-8")


def test_the_design_in_one_look() -> None:
    frame = states()["two_packages"].frame().splitlines()
    assert frame[0].startswith(" UPDATE DAY") and frame[0].endswith("Thu 08.10 04:09 ")
    assert any(line.startswith(" PACKAGE ") and line.rstrip().endswith("FROM") for line in frame)
    assert " TOTAL    1 from repositories · 1 from AUR (by hand: yay -Sua)" in frame
    assert f" MACHINE  fine · {len(CHECKS)} checks" in frame and " PENDING  nothing since last time" in frame
    assert not any(word in "\n".join(frame) for word in JARGON)
    assert sum("[y]" in line or "[N]" in line for line in frame) == 1, "only y and N are printed as keys"
    assert frame[-1].strip() == day.KEYS


@pytest.mark.parametrize("columns", [20, 30, 40, 50, 60, 72, 80, 100, 124, 200])
@pytest.mark.parametrize("lines", [16, 24, 40])
def test_no_line_of_a_frame_is_wider_than_the_screen_or_taller_than_it(columns, lines) -> None:
    page = screen(columns, lines, rows=[day.Row(f"package-with-a-rather-long-name-{n}", "1.2.3-4.5", "1.2.3-4.6", "aur") for n in range(80)])
    page.hanging = ["repo → sudo pacman -Syu" * 8]
    frame = page.frame().splitlines()
    assert max(map(len, frame)) <= min(columns, day.MAX_COLUMNS) and len(frame) <= lines - 1


def crowded(columns, lines):
    """Everything the page can hold at once: a long queue, problems, pending lines, a notice, the question and the keys."""
    page = screen(columns, lines, rows=[day.Row(f"package-{n:02d}", "1.2.3-4", "1.2.4-1", "repo") for n in range(1, 61)], bad=[("disk", "5.1 GiB free")], hanging=["repo → one", "disk → two"])
    page.notes = [day.WEEKLY_OFF]
    return page


@pytest.mark.parametrize("columns", [20, 40, 56, 100])
@pytest.mark.parametrize("lines", [3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 20, 30, 50])
def test_a_frame_is_never_taller_than_the_terminal(columns, lines) -> None:
    for page in (crowded(columns, lines), states()["problems_pending"], states()["running"], states()["failed_with_fix"], states()["unread_queue"]):
        page.resize((columns, lines))
        assert len(page.frame().splitlines()) <= lines - 1, f"{columns}x{lines}"


@pytest.mark.parametrize(("columns", "lines"), [(40, 6), (56, 9)])
def test_the_two_terminals_the_reviewers_saw_overflow_no_more(columns, lines) -> None:
    frame = crowded(columns, lines).frame().splitlines()
    assert len(frame) <= lines and frame[0].startswith(" UPDATE DAY")
    assert any(line.startswith(" TOTAL") for line in frame) and any("UPDATE NOW?" in line for line in frame), "the header, the total and the question stay"


def test_a_short_terminal_gives_up_the_footer_then_the_blank_lines_then_the_notes_then_the_rows_in_that_order() -> None:
    def gone(feature):
        """The tallest terminal on which the feature is no longer on the page."""
        return max(lines for lines in range(3, 80) if not feature("\n".join(crowded(100, lines).frame().splitlines())))

    footer = gone(lambda frame: day.KEYS in frame)
    blanks = gone(lambda frame: "\n\n" in frame)
    notes = gone(lambda frame: day.WEEKLY_OFF in frame)
    rows = gone(lambda frame: "package-01" in frame)
    status = gone(lambda frame: "MACHINE" in frame)
    assert footer > blanks > notes > rows >= status, (footer, blanks, notes, rows, status)
    one_row = crowded(100, rows + 1).frame()
    assert "package-01" in one_row and "package-02" not in one_row and "+ 59 more" in one_row, "at least one row, and how many more"


def test_a_page_that_fits_loses_nothing_to_the_rule() -> None:
    full = "\n".join(crowded(100, 100).frame().splitlines())
    assert day.KEYS in full and day.WEEKLY_OFF in full and "package-60" in full and "more" not in full and "\n\n" in full


@pytest.mark.parametrize(("columns", "present", "absent"), [(100, ("INSTALLED", "AVAILABLE"), ()), (80, ("AVAILABLE",), ("INSTALLED",)), (40, (), ("INSTALLED", "AVAILABLE"))])
def test_columns_shrink_installed_first_then_available(columns, present, absent) -> None:
    head = screen(columns, 24, rows=states()["narrow_80"].rows).frame().splitlines()[2]
    assert all(word in head for word in present) and not any(word in head for word in absent)
    assert head.rstrip().endswith("FROM") and "PACKAGE" in head


def test_more_than_the_height_shows_the_first_that_fit_then_how_many_more_and_never_scrolls() -> None:
    page = states()["overflow_40_rows"]
    frame = page.frame().splitlines()
    shown = sum(line.startswith(" package-") for line in frame)
    assert len(frame) <= 39 and shown + int(re.search(r"\+ (\d+) more", "\n".join(frame))[1]) == 60
    assert [line.split()[0] for line in frame if line.startswith(" package-")] == [f"package-{n:02d}" for n in range(1, shown + 1)]
    assert " + " not in screen(100, 80, rows=page.rows).frame(), "when everything fits there is no remainder line"


def test_colour_is_for_the_page_and_red_only_for_what_is_wrong(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR")
    red = "\x1b[38;2;208;49;45m"
    states_ = states()
    assert "\x1b[38;2;" in states_["two_packages"].frame() and red not in states_["two_packages"].frame()
    assert red in states_["problems_pending"].frame() and red in states_["unread_queue"].frame()
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in states_["problems_pending"].frame()


def test_the_pending_block_has_one_red_line_per_command(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR")
    lines = [line for line in states()["problems_pending"].frame().splitlines() if "→" in line]
    assert len(lines) == 2 and all("\x1b[38;2;208;49;45m" in line for line in lines)
    assert "arch-update auto --sudoers" in lines[0] and "sudo pacman -Syu" in lines[1]


def test_events_become_the_story_of_the_run() -> None:
    page = screen(asking=False)
    page.event("look", {"verdicts": [{"probe": "dns", "status": "fail", "detail": "archlinux.org does not resolve"}]})
    page.event("census", {"case": "DNS-FAILED", "attempted": True})
    page.event("store", {"name": "repo", "phase": "start"})
    page.event("store", {"name": "repo", "phase": "done", "changed": 17, "ok": True})
    page.event("store", {"name": "aur", "phase": "start"})
    page.event("store", {"name": "aur", "phase": "done", "changed": 0, "ok": False})
    frame = page.frame()
    assert "network: archlinux.org does not resolve" in frame and "seen before, repairing the same way" in frame
    assert "repositories  17 changed" in frame and "AUR  failed · 0 changed" in frame
    assert "PACKAGE" not in frame and "MACHINE" not in frame, "once the run starts, the story replaces the table and the status"


def test_the_running_screen_speaks_no_engine_jargon() -> None:
    assert not any(word in states()["running"].frame() for word in JARGON)


def test_on_a_terminal_a_frame_leaves_nothing_of_the_one_before() -> None:
    out = Terminal()
    screen().draw(out)
    written = out.getvalue()
    assert written.startswith(day.FRAME_START) and written.endswith(day.FRAME_END)
    lines = written.removeprefix(day.FRAME_START).removesuffix(day.FRAME_END).split("\n")[:-1]
    assert lines and all(line.endswith("\x1b[K") for line in lines), "every line erases to its end"
    assert written.count(day.FRAME_START) == 1, "one write per frame"


# ── colour and escapes only where a terminal can show them ─────────────────


@pytest.fixture
def coloured(monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "xterm-256color")


def test_a_pipe_gets_words_not_colour_codes(box, coloured) -> None:
    for kwargs in ({}, {"look_only": False}):
        waiting(box)
        out = io.StringIO()  # `arch-update | cat`
        day.ask(box.sh, io.StringIO("n\n"), out, **kwargs)
        assert "kwin" in out.getvalue() and "\x1b" not in out.getvalue()
    assert day.PLAIN is False, "the switch is put back when the visit ends"


def test_a_real_terminal_still_gets_its_colour(box, coloured) -> None:
    waiting(box)
    out = Terminal()
    with keyboard(b"n", out) as stdin:
        day.ask(box.sh, stdin, out)
    assert "\x1b[38;2;" in out.getvalue() and day.colour()


@pytest.mark.parametrize("keys", [b"n", b"y\n\n"])
def test_a_dumb_terminal_gets_no_colour_no_clear_no_synchronized_update(box, coloured, monkeypatch, keys) -> None:
    monkeypatch.setenv("TERM", "dumb")
    waiting(box)
    out = Terminal()
    with keyboard(keys, out) as stdin:
        day.ask(box.sh, stdin, out)
    text = out.getvalue()
    assert "UPDATE NOW?" in text and "\x1b" not in text, "plain text, and not one escape"
    assert text.count("UPDATE DAY") <= 4, "a dumb terminal cannot redraw: a frame is printed when its question or ending changes"
    assert ("DONE" in text) is (keys != b"n")


@pytest.mark.parametrize(("env", "value"), [("NO_COLOR", "1"), ("TERM", "dumb")])
def test_colour_is_off_for_no_color_and_for_a_dumb_terminal(coloured, monkeypatch, env, value) -> None:
    assert day.colour()
    monkeypatch.setenv(env, value)
    assert not day.colour() and "\x1b" not in screen(rows=[ZEN]).frame()


# ── reading the queue ──────────────────────────────────────────────────────


def test_the_queue_is_read_only_to_be_shown(box) -> None:
    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok(QUEUE)]
    box.sh.fail["yay -Qua"] = [failed("", 1)]
    rows, unread = day.queue(box.sh)
    assert [(row.name, row.new, row.store) for row in rows] == [("kwin", "6.7.5-3.1", "repo"), ("qt6-svg", "6.12.0-1.1", "repo")]
    assert unread == [] and box.sh.ran == []
    box.sh.fail["checkupdates"] = [failed("error: cannot fetch", 1)]
    assert day.queue(box.sh)[1] == ["repo"]


def test_paru_alone_is_read_for_the_aur_queue_and_yay_wins_when_both_are_there(box) -> None:
    box.sh.tools = {"checkupdates", "paru"}
    box.sh.fail["checkupdates"] = [ok("")]
    box.sh.fail["paru -Qua"] = [ok("discord-canary 1.0.2139-1 -> 1.0.2140-1\n")]
    rows, unread = day.queue(box.sh)
    assert [(row.name, row.store) for row in rows] == [("discord-canary", "aur")] and unread == []
    box.sh.tools.add("yay")
    box.sh.fail["checkupdates"] = [ok("")]
    box.sh.fail["yay -Qua"] = [failed("", 1)]
    box.sh.calls.clear()
    assert day.queue(box.sh) == ([], [])
    assert ["paru", "-Qua"] not in box.sh.calls, "one helper reads the queue: the first one installed"


def test_flatpak_and_snap_rows_stand_in_the_same_table(box) -> None:
    box.sh.tools |= {"checkupdates", "flatpak", "snap"}
    box.sh.fail["flatpak remote-ls"] = [ok("org.example.App\t2.0\n")]
    box.sh.fail["flatpak list"] = [ok("org.example.App\t1.0\norg.other.App\t5\n")]
    box.sh.fail["snap refresh"] = [ok("Name   Version  Rev  Size  Publisher  Notes\ncore22 20260301 2000 76MB canonical✓ base\n")]
    box.sh.fail["snap list"] = [ok("Name   Version  Rev  Tracking  Publisher  Notes\ncore22 20260101 1900 latest/stable canonical✓ base\n")]
    rows, unread = day.queue(box.sh)
    assert unread == [] and [(r.name, r.old, r.new, r.store) for r in rows] == [
        ("org.example.App", "1.0", "2.0", "flatpak"),
        ("core22", "20260101", "20260301", "snap"),
    ]
    frame = screen(rows=rows).frame()
    assert "FLATPAK" in frame and "SNAP" in frame and "1 from flatpak · 1 from snap" in frame


def test_a_store_that_is_up_to_date_adds_no_row_and_one_that_fails_is_unread(box) -> None:
    box.sh.tools |= {"checkupdates", "snap", "flatpak"}
    box.sh.fail["snap refresh"] = [ok("All snaps up to date.\n")]
    box.sh.fail["flatpak remote-ls"] = [failed("error: no remote", 1)]
    assert day.queue(box.sh) == ([], ["flatpak"])


# ── the visit ──────────────────────────────────────────────────────────────


def waiting(box, queue=QUEUE, after=""):
    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok(queue), ok(after)]
    return box


def test_nothing_waiting_is_the_same_screen_without_a_question_and_exits_0(box) -> None:
    waiting(box, queue="")
    out = io.StringIO()
    assert day.ask(box.sh, Silent(), out, look_only=False) == 0
    frame = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "TOTAL    nothing waiting" in frame and "MACHINE  fine" in frame and "PENDING  nothing since last time" in frame
    assert "UPDATE NOW?" not in frame and day.KEYS not in frame and box.sh.ran == []
    assert not (day.STATE_ROOT / "ask-result.json").exists(), "nothing was asked, so nothing is recorded as an answer"


def test_the_weekly_ask_asks_even_when_nothing_waits(box) -> None:
    waiting(box, queue="")
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("n\n"), out, look_only=False, always=True, record=True) == exits.SKIPPED
    assert "UPDATE NOW?" in out.getvalue() and json.loads((day.STATE_ROOT / "ask-result.json").read_text())["outcome"] == "declined"


def test_a_queue_that_cannot_be_read_is_never_called_nothing_waiting_and_is_never_asked_about(box) -> None:
    assert not box.sh.has("checkupdates")
    out = io.StringIO()
    assert day.ask(box.sh, Silent(), out, look_only=False) == 0, "no key is read: there is no question"
    frame = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "cannot read the repositories: install pacman-contrib" in frame and "nothing waiting" not in frame
    assert "UPDATE NOW?" not in out.getvalue() and "by hand: sudo pacman -S pacman-contrib" in frame and box.sh.ran == []
    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [failed("error: cannot fetch", 1)]
    out = io.StringIO()
    day.ask(box.sh, Silent(), out, look_only=False, always=True)
    assert "cannot read the repositories: checkupdates failed" in out.getvalue() and "UPDATE NOW?" not in out.getvalue()
    assert "by hand: checkupdates" in out.getvalue(), "the weekly question is not asked either"


def test_the_weekly_window_blocked_by_an_unread_queue_fails_its_unit_loudly(box) -> None:
    assert day.ask(box.sh, Silent(), io.StringIO(), look_only=False, always=True, record=True) == exits.BLOCKED
    assert json.loads((day.STATE_ROOT / "ask-result.json").read_text())["outcome"] == "unread"


def test_when_only_the_aur_waits_and_the_aur_is_off_nothing_is_asked(box) -> None:
    waiting(box, queue="")
    box.sh.tools.add("yay")
    box.sh.fail["yay -Qua"] = [ok("discord-canary 1.0.2139-1 -> 1.0.2140-1\n")]
    box.config(unattended_aur=False)
    out = io.StringIO()
    assert day.ask(box.sh, Silent(), out, look_only=False, always=True) == 0
    frame = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "discord-canary" in frame and "nothing for the update to do; by hand: yay -Sua" in frame
    assert "UPDATE NOW?" not in frame and box.sh.ran == []
    box.sh.fail["yay -Qua"] = [ok("discord-canary 1.0.2139-1 -> 1.0.2140-1\n")]
    box.config(unattended_aur=True)
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("n\n"), out, look_only=False)
    assert "UPDATE NOW?" in out.getvalue(), "with the AUR on, y has something to do"
    waiting(box, queue=QUEUE)
    box.sh.fail["yay -Qua"] = [ok("discord-canary 1.0.2139-1 -> 1.0.2140-1\n")]
    box.config(unattended_aur=False)
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("n\n"), out, look_only=False)
    assert "UPDATE NOW?" in out.getvalue(), "a repository package waits beside it: y updates that"


def test_without_a_terminal_the_frame_is_printed_once_and_no_key_is_read(box) -> None:
    waiting(box)
    out = io.StringIO()
    assert day.ask(box.sh, Silent(), out) == 0
    text = out.getvalue()
    assert text.count("UPDATE DAY") == 1 and "kwin" in text and "\x1b" not in text
    assert "UPDATE NOW?" not in text and day.LOOK_ONLY in text and box.sh.ran == []
    assert "+ " not in text, "a pipe has no height to run out of"


def test_a_package_that_waits_is_listed_with_where_it_comes_from(box) -> None:
    waiting(box)
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("n\n"), out, look_only=False)
    frame = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "kwin" in frame and "6.7.5-3.1" in frame and "REPO" in frame and "2 from repositories" in frame


@pytest.mark.parametrize("answer", ["\n", "n\n", "q\n", "\x1b\n", "x\n", "t\n"])
def test_anything_but_y_updates_nothing_and_is_not_now(box, answer) -> None:
    waiting(box)
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO(answer), out, look_only=False) == 0, "by hand, not now is a plain 0"
    assert box.sh.ran == [] and "not now" in out.getvalue()


def test_the_end_of_the_input_is_no_answer_not_a_not_now(box) -> None:
    waiting(box)
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO(""), out, look_only=False) == exits.TOOL_FAILURE == 70
    text = out.getvalue()
    assert "no input" in text and "not now. I will ask again" not in text and box.sh.ran == []
    waiting(box)
    assert day.ask(box.sh, io.StringIO(""), io.StringIO(), look_only=False, record=True) == 70
    record = json.loads((day.STATE_ROOT / "ask-result.json").read_text())
    assert (record["outcome"], record["exit_code"]) == ("no-input", 70), "the unit fails loudly: it was not 'declined'"
    waiting(box)
    assert day.ask(box.sh, io.StringIO("\n"), io.StringIO(), look_only=False) == 0, "Enter itself is still not now"


@pytest.mark.parametrize("answer", ["\n", "n\n", "x\n"])
def test_the_weekly_window_records_not_now_as_80_the_status_its_unit_accepts(box, answer) -> None:
    waiting(box)
    assert day.ask(box.sh, io.StringIO(answer), io.StringIO(), look_only=False, record=True) == exits.SKIPPED
    record = json.loads((day.STATE_ROOT / "ask-result.json").read_text())
    assert (record["outcome"], record["exit_code"]) == ("declined", 80)


def test_a_visit_by_hand_never_touches_the_weekly_answer_and_a_glance_leaves_no_run_folder(box) -> None:
    waiting(box)
    day.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    weekly = '{"outcome": "updated", "exit_code": 0}'
    (day.STATE_ROOT / "ask-result.json").write_text(weekly)
    assert day.ask(box.sh, io.StringIO("n\n"), io.StringIO(), look_only=False) == 0
    assert (day.STATE_ROOT / "ask-result.json").read_text() == weekly, "the owner's real answer is still there"
    waiting(box, queue="")
    assert day.ask(box.sh, Silent(), io.StringIO(), look_only=False) == 0
    assert (day.STATE_ROOT / "ask-result.json").read_text() == weekly
    assert not (box.root / "runs").exists(), "looking at the machine writes no report and no tokens"


def test_the_look_on_screen_never_runs_the_vulnerability_scan(box) -> None:
    waiting(box)
    box.patch.setattr(auto, "scan_cves", lambda: pytest.fail("arch-audit ran for a look at the screen"))
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("n\n"), out, look_only=False) == 0 and "MACHINE  fine" in out.getvalue()
    assert {item["probe"] for item in auto.look(box.sh)} >= {"privilege", "keep", "lock", "disk", "dns", "database", "machine", "smoke"}


def test_the_folder_of_a_run_exists_only_when_the_update_ran(box) -> None:
    waiting(box)
    assert day.ask(box.sh, io.StringIO("y\n\n"), io.StringIO(), look_only=False) == 0
    assert len(list((box.root / "runs").iterdir())) == 1, "the update's own folder, not one for the look as well"


def test_y_runs_the_update_here_and_ends_with_one_result_block(box) -> None:
    waiting(box)
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("y\n\n"), out, look_only=False) == 0
    assert box.sh.ran.count(UPGRADE) == 1
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "machine is fine" in final and "repositories  1 changed" in final
    assert "updated: repositories 1" in final and "kwin" in final and "6.7.5-1.1 → 6.7.5-3.1" in final and "DONE   [Enter] close" in final
    assert "IDLE" not in final and "held" not in final, "the ending is in plain words too"
    assert not any(word in out.getvalue() for word in JARGON)


def test_a_failed_update_ends_red_with_its_fix_on_screen(box) -> None:
    waiting(box)
    box.sh.fail["repo"] = [failed(CONFLICT)]
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("y\n\n"), out, look_only=False) == 30
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "FAILED" in final and 'fix: grep -h "in conflict"' in final and "are in conflict" in final


def test_a_failure_that_changed_nothing_says_so_and_does_not_claim_the_machine_is_current() -> None:
    base = {"crash": None, "trouble": [{"probe": "repo", "detail": "error: x", "fix": "y"}], "held": {}, "pacnew": [], "reboot_required": False}
    failed_first = "\n".join(day.summary({**base, "updated": {}, "exit_code": 30})[1])
    assert "nothing was changed" in failed_first and "no new versions" not in failed_first
    failed_late = "\n".join(day.summary({**base, "updated": {"repo": 2}, "exit_code": 30})[1])
    assert "updated: repositories 2" in failed_late and "nothing was changed" not in failed_late
    current = "\n".join(day.summary({**base, "trouble": [], "updated": {}, "exit_code": 0})[1])
    assert "no new versions" in current and "nothing was changed" not in current


def test_no_line_on_the_screen_waits_for_a_click_there_is_none_to_click() -> None:
    held = {"updated": {}, "crash": None, "trouble": [], "held": {"aur": "off"}, "pacnew": [], "exit_code": 0, "reboot_required": False}
    text = "\n".join([*day.summary(held)[1], *(frame for page in states().values() for frame in page.frame().splitlines())])
    assert "click" not in text and "by hand: yay -Sua" in text


def test_the_ending_names_everything_left_for_a_person() -> None:
    report = {"updated": {}, "crash": None, "trouble": [], "held": {"aur": "off", "aur_too_fresh": ["new"]}, "pacnew": ["/etc/x.pacnew"], "exit_code": 10, "reboot_required": True}
    headline, lines = day.summary(report)
    text = "\n".join(lines)
    assert headline == "DONE · REBOOT NEEDED" and "no new versions" in text
    assert "AUR waits for you; by hand: yay -Sua" in text and "held this week: new" in text and "/etc/x.pacnew" in text and "sudo pacdiff" in text
    assert "REBOOT" not in day.summary({**report, "reboot_required": False})[0]


# ── sudo ───────────────────────────────────────────────────────────────────


def test_a_password_is_asked_once_by_sudo_itself_before_the_run(box) -> None:
    waiting(box)
    box.sh.tools.add("sudo")
    box.sh.fail["sudo -n"] = [failed("sudo: a password is required", 1), failed("", 1), ok()]  # the look, the check on yes, the run's own look
    out = Terminal()
    with keyboard(b"y\n", out) as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    assert box.sh.typed == [["sudo", "-v"]], "only sudo sees the password; this program never reads it"
    assert box.sh.shared_session and box.sh.ran.count(UPGRADE) == 1
    assert "sudo asks for your password on yes" in out.getvalue()


def test_without_sudo_the_machine_line_says_yes_cannot_continue_and_not_that_sudo_asks(box) -> None:
    waiting(box)
    box.sh.fail["sudo -n"] = [failed("sudo: command not found", 127)]
    out = Terminal()
    assert not box.sh.has("sudo")
    with keyboard(b"n", out) as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "sudo is not installed: install it, y cannot continue" in final
    assert "sudo asks for your password" not in out.getvalue()


def test_the_real_look_gives_exactly_the_checks_the_frames_count(box) -> None:
    auto.CONFIG_PATH.write_text('{"conditions": [], "unattended_aur": false}')
    assert tuple(verdict["probe"] for verdict in auto.look(box.sh)) == CHECKS


def test_a_password_sudo_refuses_stops_the_visit_with_70_and_changes_nothing(box) -> None:
    waiting(box)
    box.sh.fail["sudo -n"] = [failed("", 1), failed("", 1)]
    box.sh.typed_status = 1
    out = Terminal()
    with keyboard(b"y\n", out) as stdin:
        assert day.ask(box.sh, stdin, out, record=True) == exits.TOOL_FAILURE == 70
    assert box.sh.ran == [] and "sudo did not accept the password; nothing was changed" in out.getvalue()
    assert json.loads((day.STATE_ROOT / "ask-result.json").read_text())["exit_code"] == 70


def test_without_a_terminal_a_missing_sudo_rule_stops_the_visit_with_70(box) -> None:
    waiting(box)
    box.sh.fail["sudo -n"] = [failed("", 1)] * 3
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("y\n"), out, look_only=False) == 70
    assert box.sh.ran == [] and box.sh.typed == [] and "no terminal to ask on" in out.getvalue() and "arch-update auto --sudoers" in out.getvalue()


def test_the_rule_in_sudoers_means_no_password_is_ever_asked(box) -> None:
    waiting(box)
    assert day.ask(box.sh, io.StringIO("y\n\n"), io.StringIO(), look_only=False) == 0
    assert box.sh.typed == [] and not box.sh.shared_session


def test_after_the_password_the_commands_stay_in_the_session_that_holds_sudos_ticket(monkeypatch, tmp_path) -> None:
    from arch_updater import shell

    seen = []

    class Child:
        pid = 1

        def __init__(self, argv, **kwargs):
            seen.append(kwargs)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(shell.subprocess, "Popen", Child)
    sh = Shell()
    sh.logged(["true"], tmp_path / "log")
    sh.stay_in_session()
    sh.logged(["true"], tmp_path / "log")
    assert seen[0].get("start_new_session") is True and "process_group" not in seen[0]
    assert "start_new_session" not in seen[1] and "process_group" not in seen[1], "it stays in the foreground group: Ctrl-C reaches it"


def test_a_ticket_made_earlier_in_this_terminal_still_means_the_commands_stay_in_the_session(box) -> None:
    box.sh.fail["sudo -n"] = [ok()]  # `sudo -n true` passes: a cached ticket (or a rule) - and no password prompt is needed
    assert day._root(box.sh, Terminal(), Terminal()) is True
    assert box.sh.typed == [] and box.sh.shared_session, "a passing `sudo -n true` on a terminal still takes the in-session path"


def test_without_a_terminal_a_passing_sudo_does_not_change_the_session(box) -> None:
    assert day._root(box.sh, io.StringIO(), io.StringIO()) is True
    assert not box.sh.shared_session


def test_y_while_another_run_holds_the_lock_says_so_and_changes_nothing(box) -> None:
    waiting(box)
    out = io.StringIO()
    with (box.root / "auto.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # the weekly unit, or a second Update Day, is updating
        assert day.ask(box.sh, io.StringIO("y\n"), out, look_only=False, record=True) == exits.SKIPPED
    assert "another update is running; nothing was changed" in out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert box.sh.ran == [] and "Traceback" not in out.getvalue()
    assert json.loads((day.STATE_ROOT / "ask-result.json").read_text())["outcome"] == "busy"


def test_the_ending_never_raises_on_a_report_that_lacks_a_key(box) -> None:
    waiting(box)
    with (box.root / "auto.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        skipped = auto.run_auto(sh=box.sh)
    for report in (skipped, {}, {"exit_code": 30, "trouble": [{"probe": "repo"}]}):
        day.summary(report)


# ── Ctrl-C and the signals that end the visit ──────────────────────────────


class Stub:
    """A child that was sent Ctrl-C while the program waited: wait() raises once, then the child ends by itself."""

    def __init__(self, argv, **kwargs):
        self.pid, self.waits, self.killed = 4242, 0, []

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return 130

    terminate = kill = lambda self: self.killed.append("signal")


@pytest.mark.parametrize("shared", [True, False])
def test_ctrl_c_while_a_command_runs_waits_for_it_and_never_kills_it(monkeypatch, tmp_path, shared) -> None:
    from arch_updater import shell

    child = Stub(["pacman"])
    sent = []
    monkeypatch.setattr(shell.subprocess, "Popen", lambda argv, **kwargs: child)
    monkeypatch.setattr(shell.os, "killpg", lambda pid, number: sent.append((pid, number)))
    sh = Shell()
    sh.shared_session = shared
    result = sh.logged(["pacman", "-Syu"], tmp_path / "log")
    assert sh.interrupted and result.returncode == 130 and child.waits == 2, "the child ended on its own and its status is the answer"
    assert child.killed == [] and all(number == signal.SIGINT for _, number in sent), "pacman is told what the terminal would tell it, never killed"
    assert bool(sent) is (not shared), "in the foreground group the terminal already sent it; in a session of its own it is sent from here"
    assert "[exit 130]" in (tmp_path / "log").read_text()


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_the_shell_remembers_which_signal_stopped_the_wait_and_whether_it_told_the_child(monkeypatch, tmp_path, number) -> None:
    from arch_updater import shell

    class Told(Stub):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise exits.Stopped(number)
            return -signal.SIGINT

    monkeypatch.setattr(shell.subprocess, "Popen", lambda argv, **kwargs: Told(argv))
    monkeypatch.setattr(shell.os, "killpg", lambda pid, sent: None)
    for shared in (True, False):
        sh = Shell()
        sh.shared_session = shared
        sh.logged(["pacman", "-Syu"], tmp_path / "log")
        assert (sh.interrupted, sh.signum, sh.sent) == (True, number, not shared)


@pytest.mark.parametrize(
    ("returncode", "sent", "ended"),
    [(0, True, False), (0, False, False), (1, False, False), (1, True, True), (-2, False, True), (130, False, True), (143, False, True), (129, False, True), (-1, False, True), (2, False, True)],
)
def test_a_command_is_said_to_end_by_a_signal_from_how_it_ended(returncode, sent, ended) -> None:
    assert exits.ended_by_signal(returncode, sent) is ended


def test_an_interrupted_run_releases_the_lock_and_writes_its_report(box) -> None:
    def ctrl_c(argv, log, timeout=0):
        box.sh.interrupted = True
        return failed("", 130)

    box.patch.setattr(box.sh, "logged", ctrl_c)
    report = auto.run_auto(sh=box.sh)
    assert report["state"] == "INTERRUPTED" and report["exit_code"] == exits.INTERRUPTED == 130
    assert [(item["probe"], item["case"]) for item in report["trouble"]] == [("run", "INTERRUPTED")]
    assert report["trouble"][0]["fix"] and box.sh.interrupted is False
    assert json.loads((box.root / "last-auto.json").read_text())["state"] == "INTERRUPTED"
    assert (box.root / "runs" / report["run_id"] / "report.json").is_file()
    with (box.root / "auto.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # does not raise: the lock was let go


def test_ctrl_c_during_running_ends_the_screen_cleanly_with_one_plain_line(box) -> None:
    waiting(box)

    def ctrl_c(argv, log, timeout=0):
        box.sh.interrupted = True
        return failed("", 130)

    box.patch.setattr(box.sh, "logged", ctrl_c)
    out = Terminal()
    with keyboard(b"y\n", out) as stdin:
        assert day.ask(box.sh, stdin, out) == 130
    text = out.getvalue()
    assert "interrupted; the update was stopped, run arch-update again" in text.rsplit("UPDATE DAY", 1)[1]
    assert text.endswith("\x1b[?25h"), "the cursor is back"
    assert "Traceback" not in text and "FAILED" not in text


def test_ctrl_c_at_close_after_the_update_only_closes_it(box) -> None:
    waiting(box)

    class Close(io.StringIO):
        def readline(self, *args):
            if self.tell():
                raise KeyboardInterrupt
            return super().readline(*args)

    out = io.StringIO()
    assert day.ask(box.sh, Close("y\n"), out, look_only=False) == 0
    assert "interrupted" not in out.getvalue() and "DONE" in out.getvalue(), "the update had finished: it is not reported as stopped"


def test_ctrl_c_at_the_question_leaves_the_machine_alone(box, monkeypatch) -> None:
    waiting(box)

    def ctrl_c(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(day, "read_key", ctrl_c)
    out = Terminal()
    assert day.ask(box.sh, io.StringIO(), out) == 130
    assert "interrupted; nothing was changed" in out.getvalue() and box.sh.ran == [] and out.getvalue().endswith("\x1b[?25h")


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminal_that_closes_or_a_kill_puts_the_screen_back(box, number) -> None:
    waiting(box)
    out = Terminal()
    before = signal.getsignal(number)

    def kill() -> None:
        deadline = time.monotonic() + 10
        while "UPDATE NOW?" not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        os.kill(os.getpid(), number)

    master, slave = pty.openpty()
    thread = threading.Thread(target=kill)
    thread.start()
    with os.fdopen(slave, "r") as stdin:
        assert day.ask(box.sh, stdin, out) == 128 + number
    thread.join()
    os.close(master)
    assert out.getvalue().endswith("\x1b[?25h") and box.sh.ran == [] and signal.getsignal(number) == before, "the handlers are put back"


# ── the sudo ticket during a long update ───────────────────────────────────


class Sudo:
    """Answers `sudo -n -v` from a script and counts how often it was asked; everything else just works."""

    def __init__(self, answers=()):
        self.answers, self.asked = list(answers), 0

    def capture(self, argv, timeout=30):
        assert argv == ["sudo", "-n", "-v"]
        self.asked += 1
        return ok() if (self.answers.pop(0) if self.answers else True) else failed("sudo: a password is required")


def settled(sudo):
    """The renewals have stopped: the count does not move for a while."""
    before = sudo.asked
    time.sleep(0.15)
    return sudo.asked == before


def test_the_ticket_is_renewed_while_the_block_runs_and_never_after_it() -> None:
    sudo = Sudo()
    with Keepalive(sudo, every=0.01):
        time.sleep(0.1)
        assert sudo.asked >= 3
    assert settled(sudo)


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_an_exception_or_ctrl_c_in_the_block_stops_the_renewals_and_goes_on(error) -> None:
    sudo = Sudo()
    with pytest.raises(type(error)), Keepalive(sudo, every=0.01):
        time.sleep(0.05)
        raise error
    assert settled(sudo)


def test_no_ticket_to_keep_means_no_thread_and_no_alarm() -> None:
    sudo = Sudo([False])  # `sudo -n -v` is refused from the start: sudo runs on a rule here
    with Keepalive(sudo, every=0.01) as keep:
        time.sleep(0.05)
    assert sudo.asked == 1 and not keep.expired
    assert Keepalive(None, enabled=False).__enter__().expired is False, "a disabled one never touches the machine"


def test_a_refused_renewal_is_remembered() -> None:
    sudo = Sudo([True, True, False])
    with Keepalive(sudo, every=0.01) as keep:
        time.sleep(0.1)
    assert keep.expired and sudo.asked == 3, "after the first refusal there is nothing left to renew"


def test_on_a_terminal_the_ticket_is_kept_through_the_run_and_the_ending_says_when_it_ran_out(box, monkeypatch) -> None:
    waiting(box)
    monkeypatch.setattr(day, "Keepalive", lambda sh, enabled: Keepalive(sh, enabled, every=0.01))
    asked = []
    capture = box.sh.capture

    def renew(argv, timeout=30):
        if argv == ["sudo", "-n", "-v"]:
            asked.append(argv)
            return ok() if len(asked) < 3 else failed("sudo: a password is required")
        return capture(argv, timeout)

    def slow(argv, log, timeout=0):
        time.sleep(0.15)
        return ok()

    box.patch.setattr(box.sh, "capture", renew)
    box.patch.setattr(box.sh, "logged", slow)
    out = Terminal()
    with keyboard(b"y\n\n", out) as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    assert "sudo ticket expired during the update" in out.getvalue().rsplit("UPDATE DAY", 1)[1]
    seen = len(asked)
    time.sleep(0.1)
    assert len(asked) == seen, "the thread is gone with the visit"


def test_without_a_terminal_no_ticket_is_kept(box) -> None:
    waiting(box)
    assert day.ask(box.sh, io.StringIO("y\n\n"), io.StringIO(), look_only=False) == 0
    assert ["sudo", "-n", "-v"] not in box.sh.calls


# ── one key, whole ─────────────────────────────────────────────────────────


@contextlib.contextmanager
def typed(*chunks: bytes):
    """A pty whose master types the chunks, with a pause after each; yields the slave side as a stream."""
    master, slave = pty.openpty()

    def type_them() -> None:
        time.sleep(0.2)
        for chunk in chunks:
            os.write(master, chunk)
            time.sleep(0.15)

    typist = threading.Thread(target=type_them)
    typist.start()
    stream = os.fdopen(slave, "r")
    try:
        yield stream
    finally:
        typist.join()
        stream.close()
        os.close(master)


@pytest.mark.parametrize("sequence", [b"\x1b[A", b"\x1b[B", b"\x1b[1;5C", b"\x1b[15~", b"\x1bOP", b"\x1b[3~", b"\x1bx"])
def test_a_key_that_sends_several_bytes_is_taken_whole_and_is_no_answer(sequence) -> None:
    with typed(sequence, b"l") as stream:
        assert day.read_key(stream, 5) == "l", "the arrow is swallowed, not read as '[' or taken for an answer"


def test_a_bare_esc_is_not_now_after_a_moment() -> None:
    with typed(b"\x1b") as stream:
        started = time.monotonic()
        assert day.read_key(stream, 5) == "\x1b" and time.monotonic() - started < 2


def test_ctrl_d_and_a_closed_terminal_are_the_end_of_the_input() -> None:
    with typed(b"\x04") as stream:
        assert day.read_key(stream, 5) == day.EOF_KEY
    master, slave = pty.openpty()
    os.close(master)  # the other side hung up
    with os.fdopen(slave, "r") as stream:
        assert day.read_key(stream, 5) == day.EOF_KEY


def test_an_arrow_key_is_no_not_now_and_the_next_key_still_answers(box) -> None:
    waiting(box)
    out = Terminal()
    with keyboard(b"\x1b[Ay\n\n", out) as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    assert box.sh.ran.count(UPGRADE) == 1, "'y' after the arrow was the answer; the arrow did not decline"
    assert "not now. I will ask again" not in out.getvalue()


# ── the window of the .desktop entry ───────────────────────────────────────


def test_from_the_desktop_entry_a_visit_that_updated_nothing_waits_for_enter(box, monkeypatch) -> None:
    monkeypatch.setenv(day.HOLD_ENV, "1")
    waiting(box, queue="")
    master, slave = pty.openpty()
    out, done = Terminal(), []
    with os.fdopen(slave, "r") as stdin:
        thread = threading.Thread(target=lambda: done.append(day.ask(box.sh, stdin, out)))
        thread.start()
        time.sleep(1.0)
        assert thread.is_alive() and "nothing waiting" in out.getvalue() and "[Enter] close" in out.getvalue(), "the screen stays up"
        os.write(master, b"\n")
        thread.join(5)
    os.close(master)
    assert done == [0]


@pytest.mark.parametrize("keys", [b"n\n", b"\n\n"])
def test_from_the_desktop_entry_not_now_also_waits_for_enter_and_then_closes(box, monkeypatch, keys) -> None:
    monkeypatch.setenv(day.HOLD_ENV, "1")
    waiting(box)
    out = Terminal()
    with keyboard(keys, out) as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    assert out.getvalue().count("[Enter] close") >= 1 and "not now. I will ask again" in out.getvalue()


def test_started_from_a_shell_nothing_waits_for_anything(box, monkeypatch) -> None:
    monkeypatch.delenv(day.HOLD_ENV, raising=False)
    waiting(box)
    out = Terminal()
    with keyboard(b"n", out) as stdin:  # no Enter follows: a program that waited would hang here
        assert day.ask(box.sh, stdin, out) == 0
    assert "[Enter] close" not in out.getvalue()


def test_the_hold_needs_a_terminal_to_hold(box, monkeypatch) -> None:
    monkeypatch.setenv(day.HOLD_ENV, "1")
    waiting(box)
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("n\n"), out, look_only=False) == 0 and "[Enter] close" not in out.getvalue()


def test_the_desktop_entry_sets_the_variable_that_asks_for_the_hold() -> None:
    entry = (Path(__file__).parent.parent / "assets" / "arch-update.desktop").read_text()
    assert f'Exec=env {day.HOLD_ENV}=1 "@EXEC@"' in entry.splitlines()


# ── keys ───────────────────────────────────────────────────────────────────


def test_l_shows_the_tail_of_the_last_runs_log_and_any_key_returns(box) -> None:
    waiting(box)
    run = day.STATE_ROOT / "runs" / "auto-1"
    run.mkdir(parents=True)
    (run / "run.log").write_text("".join(f"line {n}\n" for n in range(500)) + "\x1b[31mred\x1b[0m\rprogress\n")
    (day.STATE_ROOT / "last-auto.json").write_text(json.dumps({"run_id": "auto-1"}))
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("l\nx\nn\n"), out, look_only=False) == 0
    text = out.getvalue()
    assert "last run auto-1" in text and "line 499" in text and "progress" in text and "line 10\n" not in text
    assert "\x1b[31m" not in text and "[any key] back" in text and box.sh.ran == []
    assert text.rsplit("UPDATE DAY", 1)[1].count("UPDATE NOW?") == 1 or "not now" in text, "back on the screen after the key"


def test_l_shows_the_summary_of_a_run_that_stopped_at_a_check_and_wrote_no_log(box) -> None:
    waiting(box)
    report = {"run_id": "auto-2", "exit_code": 70, "updated": {}, "trouble": [{"probe": "privilege", "detail": "sudo -n needs a password", "fix": "the sudoers line"}]}
    (day.STATE_ROOT / "last-auto.json").parent.mkdir(parents=True, exist_ok=True)
    (day.STATE_ROOT / "last-auto.json").write_text(json.dumps(report))
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("l\nx\nn\n"), out, look_only=False)
    text = out.getvalue()
    assert "no run on record yet" not in text and "last run auto-2" in text
    assert "root access: sudo -n needs a password" in text and "fix: the sudoers line" in text and "FAILED" in text


@pytest.mark.parametrize("rows", [8, 12, 24])
def test_the_log_view_fits_the_terminal(rows) -> None:
    run = day.STATE_ROOT / "runs" / "auto-1"
    run.mkdir(parents=True)
    (run / "run.log").write_text("".join(f"line {n}\n" for n in range(500)))
    (day.STATE_ROOT / "last-auto.json").write_text(json.dumps({"run_id": "auto-1"}))
    assert len(day.last_log(rows)) + 2 <= rows - 1, "the lines, the blank and '[any key] back' leave a row for the cursor"


def test_l_with_no_run_on_record_says_so(box) -> None:
    waiting(box)
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("l\n\nn\n"), out, look_only=False)
    assert "no run on record yet" in out.getvalue()


def test_k_lists_the_kernel_profiles_and_installs_one_after_the_same_confirmation(box, monkeypatch) -> None:
    waiting(box)
    chosen = []
    monkeypatch.setattr(day, "available_kernel_profiles", lambda: [{"id": "auto", "label": "Keep", "package": None}, {"id": "lts", "label": "Arch LTS", "package": "linux-lts", "detected": False}])
    monkeypatch.setattr(day, "select_kernel", lambda profile: chosen.append(profile) or {"changed": True, "package": "linux-lts", "reboot_required": True})
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("k\n1\ny\n\nn\n"), out, look_only=False) == 0
    text = out.getvalue()
    assert chosen == ["lts"] and "1  Arch LTS" in text and "Keep" not in text
    assert "Install kernel profile lts and set it as the GRUB default? [y/N]" in text and "reboot needed" in text


def test_k_shows_a_file_that_could_not_be_read_as_a_plain_message(box, monkeypatch) -> None:
    waiting(box)
    monkeypatch.setattr(day, "available_kernel_profiles", lambda: [{"id": "lts", "label": "Arch LTS", "package": "linux-lts"}])

    def unreadable(profile):
        raise PermissionError(13, "Permission denied", "/etc/default/grub")

    monkeypatch.setattr(day, "select_kernel", unreadable)
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("k\n1\ny\n\nn\n"), out, look_only=False) == 0
    assert "Permission denied" in out.getvalue() and "Traceback" not in out.getvalue()


@pytest.mark.parametrize("answers", ["k\n\nn\n", "k\n9\nn\n", "k\n1\nn\n\nn\n", "k\n1\n\n\nn\n"])
def test_k_installs_nothing_unless_a_profile_is_picked_and_confirmed(box, monkeypatch, answers) -> None:
    waiting(box)
    monkeypatch.setattr(day, "available_kernel_profiles", lambda: [{"id": "lts", "label": "Arch LTS", "package": "linux-lts"}])
    monkeypatch.setattr(day, "select_kernel", lambda profile: pytest.fail("a kernel was installed"))
    assert day.ask(box.sh, io.StringIO(answers), io.StringIO(), look_only=False) == 0


def test_the_cli_kernel_asks_the_same_question_and_refuses_without_a_terminal(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli._confirm_kernel("lts") is False and "--yes" in capsys.readouterr().err
    assert day.confirm("Install?", io.StringIO("yes\n"), io.StringIO()) and not day.confirm("Install?", io.StringIO("\n"), io.StringIO())


# ── the notices under PENDING ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tools", "answer", "shown"),
    [({"systemctl"}, ok("not-found\n"), True), ({"systemctl"}, ok("loaded\n"), False), ({"systemctl"}, failed("no bus", 1), False), (set(), ok("not-found\n"), False)],
)
def test_the_weekly_line_appears_only_when_the_timer_is_known_not_to_be_installed(box, tools, answer, shown) -> None:
    waiting(box)
    box.sh.tools |= tools
    box.sh.fail["systemctl --user"] = [answer]
    out = io.StringIO()
    day.ask(box.sh, io.StringIO("n\n"), out, look_only=False)
    assert (day.WEEKLY_OFF in out.getvalue()) is shown
    assert day.WEEKLY_OFF == "weekly update is off: arch-update schedule --install-auto"


def test_no_command_opens_the_screen_and_writes_the_starter_config_with_one_plain_line(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(day, "ask", lambda **kwargs: seen.append(kwargs) or 0)
    assert not auto.CONFIG_PATH.exists()
    assert cli.main([]) == 0
    assert json.loads(auto.CONFIG_PATH.read_text()) == auto.STARTER_CONFIG
    assert seen == [{"notice": f"no config yet: wrote {auto.CONFIG_PATH}"}]
    auto.CONFIG_PATH.write_text('{"conditions": [], "unattended_aur": true}')
    seen.clear()
    assert cli.main([]) == 0 and seen == [{"notice": ""}]
    assert json.loads(auto.CONFIG_PATH.read_text())["unattended_aur"] is True, "an existing config is never touched"


def test_auto_ask_is_the_alias_that_always_asks(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(day, "ask", lambda **kwargs: seen.append(kwargs) or 0)
    assert cli.main(["auto", "--ask"]) == 0 and seen == [{"always": True, "record": False}]
    seen.clear()
    assert cli.main(["auto", "--ask", "--record"]) == 0 and seen == [{"always": True, "record": True}]


def last_run_with_no_root(trouble_case="NO-ROOT"):
    day.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    trouble = [
        {"probe": "privilege", "case": trouble_case, "fix": "the sudoers line"},
        {"probe": "repo", "case": "PACMAN-REPLACEMENT-CONFLICT", "fix": "the conflict line"},
    ]
    (day.STATE_ROOT / "last-auto.json").write_text(json.dumps({"trouble": trouble}))


@pytest.mark.parametrize("case", ["NO-ROOT", "SUDO-TIMESTAMP-EXPIRED"])
def test_a_missing_sudo_rule_is_not_pending_when_a_person_can_type_the_password(box, case) -> None:
    waiting(box)
    last_run_with_no_root(case)
    box.sh.tools.add("sudo")
    out = Terminal()
    with keyboard(b"n", out) as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "repositories → the conflict line" in final, "everything else still hangs"
    assert "root access → the sudoers line" not in final


@pytest.mark.parametrize("terminal", [False, True])
def test_the_missing_rule_stays_pending_where_nobody_can_type_a_password(box, terminal) -> None:
    waiting(box)
    last_run_with_no_root()
    if terminal:  # a terminal, but no sudo to ask the password of
        out = Terminal()
        with keyboard(b"n", out) as stdin:
            day.ask(box.sh, stdin, out)
    else:  # look only: a pipe, or the weekly unit
        out = io.StringIO()
        day.ask(box.sh, Silent(), out)
    assert "root access → the sudoers line" in out.getvalue()


def test_the_notice_stands_under_pending() -> None:
    page = screen(rows=[ZEN])
    page.notes = ["no config yet: wrote /x/auto.json"]
    lines = page.frame().splitlines()
    assert lines.index(" no config yet: wrote /x/auto.json") == next(i for i, line in enumerate(lines) if line.startswith(" PENDING")) + 1


# ── a terminal that changes size, and the waiting ──────────────────────────


def test_a_resize_redraws_the_screen_at_the_new_size(box, monkeypatch) -> None:
    waiting(box)
    monkeypatch.setattr(day.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((100, 30)))
    master, slave = pty.openpty()
    out, seen = Terminal(), []

    def shrink():
        time.sleep(0.6)
        monkeypatch.setattr(day.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((50, 20)))
        os.kill(os.getpid(), signal.SIGWINCH)
        time.sleep(0.8)
        seen.append(out.getvalue())
        os.write(master, b"n")

    thread = threading.Thread(target=shrink)
    thread.start()
    with os.fdopen(slave, "r") as stdin:
        assert day.ask(box.sh, stdin, out) == 0
    thread.join()
    os.close(master)
    cleared = seen[0].split("\x1b[2J")
    assert len(cleared) >= 3, "once at the start, once when the size changed"
    frame = cleared[-1].removeprefix(day.FRAME_START).split(day.FRAME_END)[0]
    assert max(len(line.replace("\x1b[K", "")) for line in frame.split("\n")) <= 50
    assert max(len(line.replace("\x1b[K", "")) for line in cleared[1].split("\n")) > 50, "the first frame was the wide one"


def test_an_unanswered_question_is_skipped_with_a_recorded_reason(box) -> None:
    waiting(box)
    read, write = os.pipe()
    with os.fdopen(read) as silent:
        assert day.ask(box.sh, silent, io.StringIO(), wait=0.05, look_only=False, record=True) == 80
    os.close(write)
    assert box.sh.ran == []
    record = json.loads((day.STATE_ROOT / "ask-result.json").read_text())
    assert record["outcome"] == "timed-out" and "no answer" in record["reason"]


def test_the_wait_comes_from_the_config_and_defaults_to_half_an_hour() -> None:
    assert day.ask_wait(None) == 1800 and day.ask_wait({}) == 1800
    assert day.ask_wait({"ask_timeout_minutes": 5}) == 300
    assert day.ask_wait({"ask_timeout_minutes": 0}) == 1800 and day.ask_wait({"ask_timeout_minutes": "soon"}) == 1800


# ── the weekly timer ───────────────────────────────────────────────────────


def test_the_timer_opens_the_question_only_for_an_owner_who_asked_for_it(box, monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    box.sh.tools.add("kitty")
    assert day.scheduled(box.sh) == 0 and box.sh.ran.count(UPGRADE) == 1, "not asked for: the update just runs"
    box.sh.ran.clear()
    box.config(ask=True)
    assert day.scheduled(box.sh) == 70, "the window opened and nobody answered in it"
    (window,) = box.sh.ran
    assert window[0] == "kitty" and window[-4:] == ["/usr/bin/arch-update", "auto", "--ask", "--record"]
    box.sh.tools.discard("kitty")
    box.sh.ran.clear()
    day.scheduled(box.sh)
    assert box.sh.ran.count(UPGRADE) == 1, "asked for, but there is no terminal: the week is not lost"


def test_the_window_is_never_given_a_time_limit(box, monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    box.sh.tools.add("kitty")
    box.config(ask=True)
    limits = []
    box.patch.setattr(box.sh, "logged", lambda argv, log, timeout=0: (limits.append(timeout), ok())[1])
    day.scheduled(box.sh)
    assert limits == [None]


def test_a_screen_that_cannot_draw_does_not_stop_the_update(box) -> None:
    def broken(kind, details):
        raise RuntimeError("terminal went away")

    report = auto.run_auto(sh=box.sh, watcher=broken)
    assert report["state"] == "IDLE" and json.loads((box.root / "last-auto.json").read_text())["updated"] == {"repo": 1}


@pytest.fixture
def window(box, monkeypatch):
    """A machine whose owner asked to be asked, with a terminal and a screen to open it on."""
    box.sh.tools |= {"kitty", "notify-send"}
    box.config(ask=True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    return box


def test_a_terminal_that_cannot_open_is_loud_and_the_update_is_not_silently_dropped(box, monkeypatch, capsys) -> None:
    box.sh.tools |= {"kitty", "notify-send"}
    box.config(ask=True)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert day.scheduled(box.sh) == 20
    assert box.sh.ran == [], "no window and no update: the exit status has to say so"
    assert any(call[0] == "notify-send" for call in box.sh.calls)
    assert "cannot open" in (day.STATE_ROOT / "ask-window.log").read_text()
    assert "cannot open" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ({"outcome": "updated", "exit_code": 30}, 30),
        ({"outcome": "updated", "exit_code": 10}, 10),
        ({"outcome": "declined", "exit_code": 80}, 80),
        ({"outcome": "timed-out", "exit_code": 80}, 80),
    ],
)
def test_the_timer_exits_with_the_answer_the_window_recorded_not_the_emulators(window, answer, status) -> None:
    def closed(argv, log, timeout=0):
        (day.STATE_ROOT / "ask-result.json").write_text(json.dumps(answer))
        return failed("emulator says something else", 1)

    window.patch.setattr(window.sh, "logged", closed)
    assert day.scheduled(window.sh) == status


def test_a_window_that_closes_without_an_answer_fails_the_unit_and_says_so(window) -> None:
    (day.STATE_ROOT).mkdir(parents=True, exist_ok=True)
    (day.STATE_ROOT / "ask-result.json").write_text('{"outcome": "updated", "exit_code": 0}')  # last week's answer
    window.patch.setattr(window.sh, "logged", lambda argv, log, timeout=0: ok())
    assert day.scheduled(window.sh) == 70, "a stale answer from an earlier week must not count"
    assert any(call[0] == "notify-send" for call in window.sh.calls)


def test_the_failure_block_shows_the_cause_and_not_the_exit_line(box, tmp_path) -> None:
    waiting(box)
    box.sh.fail["repo"] = [Shell().logged(["sh", "-c", "echo 'error: unresolvable package conflicts detected'; exit 1"], tmp_path / "log")]
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("y\n\n"), out, look_only=False) == 30
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "error: unresolvable package conflicts detected" in final and "[exit 1]" not in final


# ── a signal during `running …`: the exit status is the signal's, and the words follow what happened to the command ──


@pytest.mark.parametrize(
    ("number", "script", "shared", "stopped"),
    [
        (signal.SIGHUP, "sleep 5", False, True),  # the window closed; in a session of its own the update is sent Ctrl-C from here and ends by it
        (signal.SIGHUP, "sleep 1; kill -HUP $$", True, True),  # in the terminal's own group the terminal sends it the hangup too
        (signal.SIGTERM, "sleep 1", True, False),  # only the screen was killed; the command is left alone and finishes
        (signal.SIGHUP, "trap '' INT; sleep 1", False, False),  # sent Ctrl-C and finished anyway with 0
    ],
)
def test_a_signal_while_the_update_runs_ends_the_screen_with_its_number_and_tells_what_became_of_the_command(box, number, script, shared, stopped) -> None:
    waiting(box)
    plain = box.sh.logged

    def update(argv, log, timeout=0):
        if argv != UPGRADE:
            return plain(argv, log)
        box.sh.shared_session = shared  # the screen opens the sudo ticket in the person's session; here the test decides
        return Shell.logged(box.sh, ["sh", "-c", script], log)

    box.patch.setattr(box.sh, "logged", update)
    out = Terminal()

    def send() -> None:
        deadline = time.monotonic() + 10
        while "running" not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.4)
        os.kill(os.getpid(), number)

    thread = threading.Thread(target=send)
    with keyboard(b"y\n", out) as stdin:
        thread.start()
        assert day.ask(box.sh, stdin, out, record=True) == 128 + number
    thread.join()
    text = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    said = "interrupted; the update was stopped, run arch-update again" if stopped else "interrupted; the update may have finished — run arch-update to check"
    assert said in text and "DONE" not in text and text.endswith("\x1b[?25h")
    answer = json.loads((day.STATE_ROOT / "ask-result.json").read_text())
    assert (answer["outcome"], answer["exit_code"], answer["reason"]) == ("interrupted", 128 + number, said)
    report = json.loads((box.root / "last-auto.json").read_text())
    assert (report["state"], report["exit_code"], report["signal"], report["child_stopped"]) == ("INTERRUPTED", 128 + number, number, stopped)
