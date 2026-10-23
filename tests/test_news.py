import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from arch_updater import day, exits, news, watch
from arch_updater.main import main
from arch_updater.shell import Shell


def post(title: str, days_ago: float, body: str = "") -> str:
    when = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    slug = title.replace(" ", "-")
    return f"<item><title>{title}</title><link>https://archlinux.org/news/{slug}/</link><description>{body}</description><pubDate>{when}</pubDate><guid>{title}</guid></item>"


def feed(*items: str) -> str:
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Arch</title>{"".join(items)}</channel></rss>'


class Wire(Shell):
    def __init__(self, document: str | Exception) -> None:
        super().__init__()
        self.document, self.asked = document, 0

    def get(self, url, limit=1 << 20, timeout=30):
        self.asked += 1
        if isinstance(self.document, Exception):
            raise self.document
        return self.document


FIRST = "Manual intervention required for foo 2.0"
MANUAL = post(FIRST, 1)
CALM = post("Make your voice heard in the survey", 1)


def scene(sh: Shell) -> watch.Scene:
    return watch.Scene(sh=sh, config={}, config_path=None, keep=None, baseline=None)


def test_only_a_post_that_asks_for_a_manual_step_is_taken() -> None:
    items = news.parse(feed(MANUAL, CALM, post("Update", 2, body="Please do the following before upgrading: stop the service")))
    assert [item.title for item in items] == [FIRST, "Update"]


def test_a_feed_that_is_not_a_feed_is_a_value_error_not_an_empty_list() -> None:
    for broken in ("<html>", "", "<rss><nothing/></rss>"):
        with pytest.raises(ValueError):
            news.parse(broken)


def test_the_first_look_marks_what_was_already_there_as_read(tmp_path) -> None:
    assert news.waiting(Wire(feed(MANUAL)), tmp_path) == []
    assert news.seen(tmp_path) == {FIRST}


def test_a_new_post_waits_until_it_is_acknowledged_and_an_old_one_never_does(tmp_path) -> None:
    news.acknowledge([], tmp_path)  # a look was made before: the next post is news
    old = post("Manual intervention for bar", news.WINDOW_DAYS + 5)
    waiting = news.waiting(Wire(feed(MANUAL, CALM, old)), tmp_path)
    assert [item.title for item in waiting] == [FIRST]
    news.acknowledge(waiting, tmp_path)
    assert news.waiting(Wire(feed(MANUAL, CALM, old)), tmp_path) == []


def test_the_probe_blocks_on_unread_manual_news_and_says_how_to_go_on() -> None:
    news.acknowledge([], watch.STATE_ROOT)
    [verdict] = watch.probe_news(scene(Wire(feed(MANUAL)))).verdicts
    assert (verdict.status, verdict.case, verdict.code) == ("blocked", "ARCH-NEWS-MANUAL", exits.BLOCKED)
    assert "foo 2.0" in verdict.detail and "arch-update news --ack" in verdict.detail


def test_a_dead_news_server_stops_nothing() -> None:
    for failure in (OSError("no route"), ValueError("not xml")):
        [verdict] = watch.probe_news(scene(Wire(failure))).verdicts
        assert verdict.status == "pass" and "not read" in verdict.detail


def test_the_news_probe_is_a_gate_the_timer_reads_before_it_opens_a_window() -> None:
    assert watch.probe_news in watch.GATES and watch.probe_news in watch.PROBES


def test_the_timer_run_skips_loudly_while_news_waits(monkeypatch) -> None:
    said = []
    monkeypatch.setattr(day, "_say", lambda sh, message: said.append(message))
    held = watch.Verdict("news", "blocked", "ARCH-NEWS-MANUAL", "foo (https://archlinux.org/news/foo/)", exits.BLOCKED)
    monkeypatch.setattr(day.auto, "hold", lambda sh, config: held)
    monkeypatch.setattr(day.auto, "run_auto", lambda **kw: pytest.fail("the update must not start"))
    assert day.scheduled(Wire(feed())) == exits.SKIPPED
    assert said and "foo" in said[0] and "not run" in said[0]


def test_the_command_lists_then_acknowledges(monkeypatch, capsys) -> None:
    news.acknowledge([], watch.STATE_ROOT)
    monkeypatch.setattr(Shell, "get", lambda self, url, limit=1 << 20, timeout=30: feed(MANUAL))
    assert main(["news"]) == 0
    assert "foo 2.0" in capsys.readouterr().out
    assert main(["news", "--ack"]) == 0
    capsys.readouterr()
    assert main(["news"]) == 0
    assert "no manual-step news waiting" in capsys.readouterr().out
    assert json.loads((watch.STATE_ROOT / news.SEEN).read_text()) == [FIRST]


def test_a_run_that_looks_again_does_not_ask_the_news_server_again(tmp_path) -> None:
    news.acknowledge([], tmp_path)
    sh = Wire(feed(MANUAL))
    news.waiting(sh, tmp_path)
    news.waiting(sh, tmp_path)
    assert sh.asked == 1
    news.waiting(sh, tmp_path, now=time.time() + news.CACHE_SECONDS + 1)
    assert sh.asked == 2, "a cache older than its time asks again"


def test_a_page_that_is_not_a_feed_is_not_kept(tmp_path) -> None:
    sh = Wire("<html>captive portal</html>")
    for _ in range(2):
        with pytest.raises(ValueError):
            news.waiting(sh, tmp_path)
    assert sh.asked == 2 and not (tmp_path / news.CACHE).exists()
