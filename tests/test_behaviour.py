"""Behaviour of the cards, the menu data and the shared surface helpers."""

from __future__ import annotations

import json
from pathlib import Path

from arch_updater import repairs
from arch_updater.engine import VAULT_PATH, load_json
from arch_updater.preview import (
    ACTION_SLOTS,
    ANSI_RE,
    fzf_records_for_query,
    package_fzf_records,
    render_chip_hud,
    render_help,
    render_package_preview,
    render_runway,
    render_storefront,
    storefront_fzf_records,
    write_deck_book,
)
from arch_updater.share_report import sanitize_patrol_report
from arch_updater.surface import (
    HISTORY_CAP,
    READ_ONLY_LINE,
    SCHEDULE_BOUNDARY,
    TRUECOLOR_HEX,
    append_patrol_history,
    confirm_preflight_text,
    format_argv,
    load_patrol_history,
    parse_queue_versions,
    render_share_preview,
)
from arch_updater.ui import BOOK_REFRESH_IDS, MUTATING_IDS, menu_fzf_argv, render_plan, render_schedule_page, selectable_actions

ENGINE_STAGES = "Repos → AUR → Attest"
MAIN_SCREENS = ("Updater Engine", "Plan", "Runway", "Schedule")


def _plain(text: str) -> str:
    return ANSI_RE.sub("", text)


def test_a_failed_deck_book_write_keeps_the_previous_book(tmp_path: Path, monkeypatch) -> None:
    book = tmp_path / "deck-book.json"
    monkeypatch.setattr("arch_updater.preview.DECK_BOOK_PATH", book)
    monkeypatch.setattr("arch_updater.preview.DECK_BOOK_LOG_PATH", tmp_path / "deck-book.log")
    write_deck_book({"status": "old-valid\n"}, 1.0)
    previous = book.read_text(encoding="utf-8")
    original = Path.replace

    def boom(self, target):  # type: ignore[no-untyped-def]
        if str(self).endswith(".tmp"):
            raise OSError("disk full")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", boom)
    try:
        write_deck_book({"status": "new-broken\n"}, 1.0)
        raise AssertionError("replace should fail")
    except OSError:
        pass
    assert book.read_text(encoding="utf-8") == previous
    assert "old-valid" in previous


def test_shown_commands_are_safe_to_paste_into_fish() -> None:
    shown = format_argv(["pacman", "-Syu", "--", "linux-zen"])
    assert "pacman" in shown
    assert "(" not in shown
    assert "$" not in shown
    assert "`" not in shown


def test_a_large_queue_cannot_hide_a_pending_reboot_or_a_pacman_lock() -> None:
    fat = {"pacman_lock": False, "updates": {"repo": 400, "aur": 90, "repo_packages": ["p"] * 4}, "root": {}}
    reboot = render_runway(fat, {"reboot_required": True, "healthy": True})
    locked = render_runway({**fat, "pacman_lock": True}, {"reboot_required": False, "healthy": True})
    assert "REBOOT FIRST" in reboot
    assert "READY TO REVIEW" not in reboot
    assert "BLOCKED" in locked
    assert "READY TO REVIEW" not in locked


def test_package_search_finds_the_kernel_and_the_storefront_shelves() -> None:
    assert "pkg:kernel:linux-zen" in fzf_records_for_query("zen")
    empty = storefront_fzf_records("")
    assert "Official repo" in empty
    assert "Your update shelf" in empty
    assert package_fzf_records("linux-zen")


def test_ready_to_review_names_the_facts_that_would_overturn_it() -> None:
    text = render_runway(
        {"pacman_lock": False, "updates": {"repo": 1, "aur": 0}, "root": {}},
        {"reboot_required": False, "healthy": True},
    )
    assert "READY TO REVIEW" in text
    assert "pacman_lock" in text
    assert "reboot_required" in text
    assert "overturns" in text


def test_the_confirmation_shows_observed_state_then_the_command() -> None:
    a = confirm_preflight_text({"pending": {"repo": 1, "aur": 0}, "pacman_lock": False, "reboot_required": False}, ["sudo", "pacman", "-Syu"])
    b = confirm_preflight_text({"pending": {"repo": 9, "aur": 2}, "pacman_lock": False, "reboot_required": False}, ["sudo", "pacman", "-Syu"])
    assert "observed:" in a
    assert "mutate:" in a
    assert "repo=1" in a
    assert "repo=9" in b
    assert format_argv(["sudo", "pacman", "-Syu"]) in a
    assert a.split("mutate:")[1] == b.split("mutate:")[1]


def test_only_preview_writes_the_deck_book() -> None:
    root = Path(__file__).resolve().parents[1] / "arch_updater"
    writers = []
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "DECK_BOOK_PATH.write" in text or "DECK_BOOK_PATH.open(" in text:
            writers.append(path.name)
    assert writers == ["preview.py"]


def test_the_plan_rationale_follows_the_pacman_lock() -> None:
    unlocked = render_plan(
        {
            "mode": "full",
            "commands": [["sudo", "pacman", "-Syu"]],
            "mutation_boundary": "Deck never reboots.",
            "preflight": {
                "pacman_lock": False,
                "root_free_bytes": 40 * 1024**3,
                "pending_updates": {"repo": 3, "aur": 1},
                "reboot_required": False,
            },
        }
    )
    locked = render_plan(
        {
            "mode": "full",
            "commands": [["sudo", "pacman", "-Syu"]],
            "mutation_boundary": "Deck never reboots.",
            "preflight": {
                "pacman_lock": True,
                "root_free_bytes": 40 * 1024**3,
                "pending_updates": {"repo": 3, "aur": 1},
                "reboot_required": False,
            },
        }
    )
    assert "Engine is ready" in unlocked
    assert "Engine is ready" not in locked
    assert "pacman lock" in locked.lower() or "waits" in locked


def test_help_and_plan_name_the_main_screens() -> None:
    help_text = render_help()
    plan = render_plan(
        {
            "mode": "full",
            "commands": [["sudo", "pacman", "-Syu"]],
            "mutation_boundary": "x",
            "preflight": {"pacman_lock": False, "root_free_bytes": 1, "pending_updates": {}, "reboot_required": False},
        }
    )
    blob = help_text + plan
    for name in MAIN_SCREENS:
        assert name in blob


def test_the_schedule_page_labels_history_as_history() -> None:
    text = render_schedule_page({"patrol": {}, "manager": "running", "cadence": "daily"})
    assert "persisted historical report" in text
    assert "live now" not in text


def test_a_pacman_lock_shows_in_the_chip_hud() -> None:
    chips = render_chip_hud({"updates": {"repo_packages": ["a"] * 9, "aur_packages": []}, "pacman_lock": True, "root": {}}, None)
    assert "lock" in chips


def test_the_card_underlay_and_canvas_colours() -> None:

    assert TRUECOLOR_HEX["underlay"] == "#0c1118"
    assert TRUECOLOR_HEX["canvas"] == "#07090d"


def test_book_refresh_ids_are_not_mutating() -> None:
    assert BOOK_REFRESH_IDS.isdisjoint(MUTATING_IDS)


def test_separators_are_not_commands() -> None:
    seps = [item_id for item_id, _t, _s in ACTION_SLOTS if item_id.startswith("_sep")]
    selectable = {item[0] for item in selectable_actions()}
    assert seps
    assert set(seps).isdisjoint(selectable)
    assert set(seps).isdisjoint(MUTATING_IDS)


def test_the_core_cards_carry_the_read_only_line() -> None:
    runway = render_runway({"pacman_lock": False, "updates": {}, "root": {}}, {"reboot_required": False, "healthy": True})
    plan = render_plan(
        {
            "mode": "full",
            "commands": [["sudo", "pacman", "-Syu"]],
            "mutation_boundary": "x",
            "preflight": {"pacman_lock": False, "root_free_bytes": 1, "pending_updates": {}, "reboot_required": False},
        }
    )
    schedule = render_schedule_page({"patrol": {"installed": True, "active": True}, "live": True})
    help_text = render_help()
    for text in (runway, plan, schedule, help_text):
        assert READ_ONLY_LINE in text


def test_an_aur_package_card_is_not_a_trust_signal() -> None:
    aur = render_package_preview("aur", "yay", "Name : yay\nVersion : 1\nDescription : pkg\n")
    assert "AUR" in aur
    assert "not a trust signal" in aur
    assert "official catalog" not in aur
    assert "trusted" not in aur.lower()


def test_the_plan_shows_the_engine_stages() -> None:
    plan = render_plan(
        {
            "mode": "full",
            "commands": [["sudo", "pacman", "-Syu"]],
            "mutation_boundary": "x",
            "preflight": {"pacman_lock": False, "root_free_bytes": 1, "pending_updates": {}, "reboot_required": False},
        }
    )
    assert ENGINE_STAGES in plan


def test_blocked_is_readable_without_colour() -> None:
    text = _plain(render_runway({"pacman_lock": True, "updates": {}, "root": {}}, {"reboot_required": False, "healthy": True}))
    assert "BLOCKED" in text


def test_the_schedule_boundary_is_in_the_subtitle_and_the_card() -> None:
    assert any(SCHEDULE_BOUNDARY in slot[2] for slot in ACTION_SLOTS if slot[0] == "schedule")
    assert SCHEDULE_BOUNDARY in render_schedule_page({"patrol": {}})


def test_the_menu_returns_focus_to_the_selected_item() -> None:
    argv = menu_fzf_argv("Deck", selected_id="plan")
    assert argv is not None
    binds = [argv[i + 1] for i, value in enumerate(argv) if value == "--bind"]
    assert any("pos(" in item for item in binds)


def test_patrol_history_is_capped_and_a_broken_file_reads_as_empty(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "patrol-history.json"
    monkeypatch.setattr("arch_updater.surface.HISTORY_PATH", path)
    path.write_text("{not json", encoding="utf-8")
    assert load_patrol_history() == []
    for index in range(20):
        append_patrol_history({"finished_at": str(index), "healthy": True, "pending": {"repo": 0, "aur": 0}, "cve_findings": 0})
    history = load_patrol_history()
    assert len(history) == HISTORY_CAP
    assert history[0]["finished_at"] == "19"


def test_the_share_preview_forbids_identity() -> None:
    report = {
        "finished_at": "now",
        "hostname": "private-host",
        "snapshot": {"updates": {"repo": 1, "aur": 2}},
        "verification": {"healthy": True, "reboot_required": False, "issues": []},
        "cve": {"available": True, "findings": [{"package": "secret-package"}]},
    }
    sanitized = sanitize_patrol_report(report)
    text = render_share_preview(sanitized)
    assert "has not sent anything" in text
    assert "private-host" not in text
    assert "secret-package" not in json.dumps(sanitized)


def test_a_package_card_shows_old_and_new_versions() -> None:
    text = render_package_preview("repo", "linux", "Name : linux\nVersion : 6.2\nDescription : linux 6.1-1 -> 6.2-1\n")
    assert "old 6.1-1" in text
    assert "new 6.2-1" in text
    assert parse_queue_versions("linux 6.1-1 -> 6.2-1") == ("6.1-1", "6.2-1")


def test_the_storefront_has_its_shelves() -> None:
    text = render_storefront({"updates": {"repo_packages": ["a"], "aur_packages": ["b"]}})
    assert "Your update shelf" in text
    assert "Official repo" in storefront_fzf_records("")


def test_every_automatic_vault_mode_has_a_repair_that_exists() -> None:
    modes = load_json(VAULT_PATH)["modes"]
    assert modes
    for mode in modes:
        repair = mode.get("repair")
        assert repair is None or repair in repairs.ACTIONS, f"{mode['id']} names a repair that does not exist: {repair}"
        assert bool(mode.get("automatic")) == (repair is not None), f"{mode['id']}: automatic and repair disagree"
