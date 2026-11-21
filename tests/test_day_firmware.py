"""Firmware waiting on LVFS is listed on Update Day and never flashed by it."""

# ruff: noqa: F811
from __future__ import annotations

import json

from test_auto import box, failed, ok  # noqa: F401 - box is a fixture
from test_day import screen

from arch_updater import day

DEVICES = {
    "Devices": [
        {"Name": "UEFI dbx", "Version": "371", "Releases": [{"Version": "413"}]},
        {"Name": "\x1b]0;owned\x07Dock\x1b[31m", "Version": "1.0", "Releases": []},
    ]
}


def firmware(box, answer):
    box.sh.tools = {"fwupdmgr"}
    box.sh.fail["fwupdmgr get-updates"] = [answer]
    rows, unread = day.queue(box.sh)
    return rows, [store for store in unread if store != "repo"]  # no checkupdates here: the repository queue is not what is tested


def test_a_device_with_a_newer_release_is_a_row_and_a_name_cannot_write_to_the_terminal(box) -> None:
    rows, unread = firmware(box, ok(json.dumps(DEVICES)))
    assert unread == []
    assert [(row.name, row.old, row.new, row.store) for row in rows] == [("UEFI dbx", "371", "413", "firmware"), ("Dock", "1.0", "?", "firmware")]
    assert all("\x1b" not in row.name and "\x07" not in row.name for row in rows)
    assert ["fwupdmgr", "get-updates", "--json", "--no-metadata-check"] in box.sh.calls
    assert box.sh.ran == [], "reading the list changes nothing"


def test_nothing_to_do_is_an_empty_list_not_an_unread_one(box) -> None:
    assert firmware(box, failed("", 2)) == ([], [])


def test_a_broken_answer_is_an_unread_store_not_an_empty_one(box) -> None:
    assert firmware(box, ok("not json"))[1] == ["firmware"]
    assert firmware(box, failed("", 1))[1] == ["firmware"]


def test_a_machine_without_fwupd_has_no_firmware_line(box) -> None:
    box.sh.tools = set()
    assert day.queue(box.sh)[0] == [] and not any(call[:1] == ["fwupdmgr"] for call in box.sh.calls)


def test_the_screen_says_by_hand_and_names_the_command() -> None:
    frame = screen(rows=[day.Row("UEFI dbx", "371", "413", "firmware")]).frame()
    assert "FIRMWARE · BY HAND" in frame
    assert "1 from firmware (firmware by hand: fwupdmgr update)" in frame
