"""The update-day screen: what it shows, what a key does, what the timer starts."""

import io
import json
import os

import pytest

from arch_updater import auto, day
from test_auto import CONFLICT, UPGRADE, box, failed, ok  # noqa: F401 - box is a fixture

PROGRAM = auto.PROGRAM.read_text(encoding="utf-8")
QUEUE = "kwin 6.7.5-1.1 -> 6.7.5-3.1\nqt6-svg 6.11.2-1.1 -> 6.12.0-1.1\n"


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")


JARGON = ("@CTL", "@FIX", "ACC", "WATCH", "CENSUS", "MOV ", "PORTS")


def screen(columns=118, lines=36, engine=False):
    page = day.Screen(PROGRAM, (columns, lines))
    page.engine = engine
    return page


def test_the_listing_is_the_program_as_written(plain) -> None:
    code = day.listing(PROGRAM)
    assert list(code) == ["CTL", "FIX"] and len(code["CTL"]) == 12 and len(code["FIX"]) == 9
    assert code["CTL"][0] == ("WATCH:  MOV UP ACC", "look at the machine as it is now")
    parsed = auto.tis.parse(PROGRAM)
    assert all(len(code[name]) == len(parsed[name].code) for name in code), "one drawn line per instruction, or the highlight lies"


@pytest.mark.parametrize("engine", [False, True])
@pytest.mark.parametrize(("columns", "lines"), [(72, 20), (90, 24), (100, 30), (118, 36), (200, 60)])
def test_no_line_of_a_frame_is_wider_than_the_screen(plain, columns, lines, engine) -> None:
    page = screen(columns, lines, engine)
    page.rows = [day.Row(f"package-with-a-rather-long-name-{n}", "1.2.3-4.5", "1.2.3-4.6", "repo") for n in range(80)]
    page.verdicts = [{"probe": name, "status": "pass", "detail": ""} for name in ("privilege", "keep", "lock", "disk", "dns", "database", "machine", "smoke")]
    page.hanging = ["repo → sudo pacman -Syu" * 6]
    page.prompt = "UPDATE NOW?"
    frame = page.frame().splitlines()
    assert max(map(len, frame)) <= page.columns and len(frame) <= lines


def test_the_table_is_what_waits_and_says_what_is_left_to_the_owner(plain) -> None:
    page = screen()
    page.rows = [day.Row("kwin", "6.7.5-1.1", "6.7.5-3.1", "repo"), day.Row("example-app-git", "1.0.2134-1", "1.0.2136-1", "aur")]
    frame = page.frame()
    assert "kwin" in frame and "6.7.5-3.1" in frame and "AUR · BY HAND" in frame
    assert "1 from repositories · 1 from AUR (wait for your click)" in frame
    page.aur_on = True
    assert "BY HAND" not in page.frame() and "wait for your click" not in page.frame()


def test_every_label_stands_apart_from_its_value(plain) -> None:
    page = screen(engine=True)
    page.rows = [day.Row("kwin", "1", "2", "repo")]
    page.verdicts = [{"probe": "dns", "status": "pass", "detail": ""}]
    page.event("token", {"source": "WATCH", "target": "CTL", "value": 1})
    page.log.append("x")
    frame = page.frame()
    assert " MACHINE  fine · 1 checks" in frame and " PENDING  nothing since last time" in frame and " PORTS    WATCH 1" in frame
    page.log.clear()
    assert " TOTAL    1 from repositories" in page.frame()


def test_a_frame_before_anything_is_read_says_so(plain) -> None:
    frame = screen().frame()
    assert "reading the queue" in frame and "checking" in frame
    assert not any(word in frame for word in JARGON), "the default screen is for someone who never heard of the engine"
    assert "@CTL" in screen(engine=True).frame() and "@FIX" in screen(engine=True).frame()


def test_a_narrow_terminal_drops_the_nodes_not_the_table(plain) -> None:
    page = screen(80, 24, engine=True)
    page.rows = [day.Row("kwin", "1", "2", "repo")]
    assert "@CTL" not in page.frame() and "kwin" in page.frame()


def test_the_node_shows_where_the_program_is_and_its_acc(plain) -> None:
    page = screen(engine=True)
    page.step("CTL", 7, 1)
    page.step("FIX", 1, 3)
    frame = page.frame()
    assert "▶GREEN:  MOV RIGHT ACC" in frame and "▶NEXT:   JEZ SPENT" in frame
    assert frame.count("▶") == 2 and "ACC    1" in frame and "ACC    3" in frame


def test_events_become_the_story_of_the_run(plain) -> None:
    page = screen()
    page.event("look", {"verdicts": [{"probe": "dns", "status": "fail", "detail": "archlinux.org does not resolve"}]})
    page.event("token", {"source": "WATCH", "target": "CTL", "value": -19})
    page.event("census", {"case": "DNS-FAILED", "attempted": True})
    page.event("store", {"name": "repo", "phase": "start"})
    page.event("store", {"name": "repo", "phase": "done", "changed": 17, "ok": True})
    page.event("store", {"name": "aur", "phase": "start"})
    page.event("store", {"name": "aur", "phase": "done", "changed": 0, "ok": False})
    frame = page.frame()
    assert "network: archlinux.org does not resolve" in frame and "seen before, repairing the same way" in frame
    assert "repositories  17 changed" in frame and "AUR  failed · 0 changed" in frame
    assert "PACKAGE" not in frame, "once the run starts, the story replaces the table"
    assert "WATCH" not in frame and "PORTS" not in frame
    page.engine = True
    assert "WATCH -19" in page.frame()


def test_on_a_terminal_a_frame_leaves_nothing_of_the_one_before(plain) -> None:
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    out = Terminal()
    page = screen()
    page.draw(out)
    written = out.getvalue()
    assert written.startswith(day.FRAME_START) and written.endswith(day.FRAME_END)
    lines = written.removeprefix(day.FRAME_START).removesuffix(day.FRAME_END).split("\n")[:-1]
    assert lines and all(line.endswith("\x1b[K") for line in lines), "every line erases to its end"


def test_the_queue_is_read_only_to_be_shown(box) -> None:
    box.sh.tools.add("checkupdates")
    box.sh.fail["checkupdates"] = [ok(QUEUE)]
    box.sh.fail["yay -Qua"] = [failed("", 1)]
    rows, unread = day.queue(box.sh)
    assert [(row.name, row.new, row.store) for row in rows] == [("kwin", "6.7.5-3.1", "repo"), ("qt6-svg", "6.12.0-1.1", "repo")]
    assert unread == [] and box.sh.ran == []
    box.sh.fail["checkupdates"] = [failed("error: cannot fetch", 1)]
    assert day.queue(box.sh)[1] == ["repo"]


@pytest.mark.parametrize("answer", ["\n", "n\n", "x\n", ""])
def test_anything_but_y_updates_nothing(box, plain, answer) -> None:
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO(answer), out) == 0
    assert box.sh.ran == [] and "not now" in out.getvalue()


def test_y_runs_the_update_here_and_ends_on_its_state(box, plain) -> None:
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("y\n\n"), out) == 0
    assert box.sh.ran.count(UPGRADE) == 1
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "machine is fine" in final and "repositories  2 changed" in final
    assert "updated: repositories 2" in final and "DONE   [Enter] close" in final
    assert "IDLE" not in final and "held" not in final, "the ending is in plain words too"
    assert not any(word in out.getvalue() for word in JARGON)


def test_t_shows_the_engine_and_asks_again(box, plain) -> None:
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("t\ny\n\n"), out) == 0
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "▶HALT:   MOV ACC DOWN" in final and "@FIX" in final and box.sh.ran.count(UPGRADE) == 1


def test_a_failed_update_ends_red_with_its_fix_on_screen(box, plain) -> None:
    box.sh.fail["repo"] = [failed(CONFLICT)]
    out = io.StringIO()
    assert day.ask(box.sh, io.StringIO("y\n\n"), out) == 30
    final = out.getvalue().rsplit("UPDATE DAY", 1)[1]
    assert "FAILED" in final and "fix: sudo pacman -Syu" in final and "are in conflict" in final


def test_the_timer_opens_the_question_only_for_an_owner_who_asked_for_it(box, monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    box.sh.tools.add("kitty")
    assert day.scheduled(box.sh) == 0 and box.sh.ran.count(UPGRADE) == 1, "not asked for: the update just runs"
    box.sh.ran.clear()
    box.config(ask=True)
    assert day.scheduled(box.sh) == 70, "the window opened and nobody answered in it"
    (window,) = box.sh.ran
    assert window[0] == "kitty" and window[-3:] == ["/usr/bin/arch-update", "auto", "--ask"]
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
    assert report["state"] == "IDLE" and json.loads((box.root / "last-auto.json").read_text())["updated"] == {"repo": 2}


def test_the_ending_names_everything_left_for_a_person(plain) -> None:
    report = {"updated": {}, "crash": None, "trouble": [], "held": {"aur": "off", "aur_too_fresh": ["new"]}, "pacnew": ["/etc/x.pacnew"], "exit_code": 10, "reboot_required": True}
    headline, lines = day.summary(report)
    text = "\n".join(lines)
    assert headline == "DONE · REBOOT NEEDED" and "no new versions" in text
    assert "arch-update run --mode aur" in text and "held this week: new" in text and "/etc/x.pacnew" in text and "sudo pacdiff" in text


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


def test_an_unanswered_question_is_skipped_with_a_recorded_reason(box, plain) -> None:
    read, write = os.pipe()
    with os.fdopen(read) as silent:
        assert day.ask(box.sh, silent, io.StringIO(), wait=0.05) == 80
    os.close(write)
    assert box.sh.ran == []
    record = json.loads((day.STATE_ROOT / "ask-result.json").read_text())
    assert record["outcome"] == "timed-out" and "no answer" in record["reason"]


def test_the_wait_comes_from_the_config_and_defaults_to_half_an_hour() -> None:
    assert day.ask_wait(None) == 1800 and day.ask_wait({}) == 1800
    assert day.ask_wait({"ask_timeout_minutes": 5}) == 300
    assert day.ask_wait({"ask_timeout_minutes": 0}) == 1800 and day.ask_wait({"ask_timeout_minutes": "soon"}) == 1800


def test_without_checkupdates_the_screen_says_it_cannot_check_not_that_nothing_waits(box, plain) -> None:
    assert not box.sh.has("checkupdates")
    rows, unread = day.queue(box.sh)
    assert rows == [] and unread == ["repo"]
    page = screen()
    page.rows, page.unread = rows, unread
    frame = page.frame()
    assert "cannot check the repositories: install pacman-contrib" in frame
    assert "nothing is waiting" not in frame
