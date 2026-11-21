"""The size of what the repository queue downloads is shown in TOTAL, and an unread size is never shown as zero."""

# ruff: noqa: F811
from __future__ import annotations

from test_auto import box, failed, ok  # noqa: F401 - box is a fixture
from test_day import screen

from arch_updater import day, space

SI = """\
Repository      : extra
Name            : kwin
Download Size   : 6.5 MiB
Installed Size  : 21.0 MiB

Repository      : core
Name            : qt6-svg
Download Size   : 512.0 KiB
Installed Size  : 1.0 MiB
"""


def test_the_download_is_the_sum_of_the_waiting_packages(box) -> None:
    box.sh.fail["env LC_ALL=C"] = [ok(SI)]
    assert space.download_total(box.sh, ["kwin", "qt6-svg"]) == int(6.5 * 1024**2) + 512 * 1024
    assert any(call[:4] == ["env", "LC_ALL=C", "pacman", "-Si"] and str(space.checkup_db()) in call for call in box.sh.calls)
    assert box.sh.ran == []


def test_an_unreadable_or_incomplete_answer_is_unknown_not_zero(box) -> None:
    box.sh.fail["env LC_ALL=C"] = [failed("error: package not found", 1)]
    assert space.download_total(box.sh, ["kwin"]) is None
    box.sh.fail["env LC_ALL=C"] = [ok(SI)]
    assert space.download_total(box.sh, ["kwin", "missing"]) is None


def test_total_shows_the_size_only_when_it_is_known() -> None:
    rows = [day.Row("kwin", "6.7.4-1", "6.7.5-3", "repo")]
    page = screen(rows=rows)
    assert "to download" not in page.frame()
    page.download = int(6.5 * 1024**2)
    assert "1 from repositories · 6 MiB to download" in page.frame()
