from pathlib import Path
from types import SimpleNamespace

from arch_updater.preview import (
    ANSI_RE,
    GREEN,
    UNKNOWN_SOURCE,
    _live_subtitle,
    aur_hover_info,
    action_catalog,
    load_deck_book_card,
    fzf_records_for_query,
    marquee,
    menu_fzf_records,
    package_fzf_records,
    parse_si,
    render_action_preview,
    render_deck_book_preview,
    render_attest_board,
    render_package_preview,
    render_runway,
    render_status_badge,
    queue_counts_from_records,
    render_chip_hud,
    render_storefront,
    render_store_source,
    render_slot,
    storefront_fzf_records,
    write_deck_book,
    write_hud_cache,
)
from arch_updater.preview import _name_rank
from arch_updater.ui import MENU_ACTIONS, fzf_menu_items


def test_fzf_catalog_starts_on_help() -> None:
    rows = fzf_menu_items(MENU_ACTIONS)
    assert rows[0][0] == "help"
    assert "status" in {row[0] for row in rows}


def test_slot_is_two_lines_without_fill() -> None:
    block = render_slot("Status", "kernel, disk, GPU, queue, ABI")
    lines = block.split("\n")
    assert len(lines) == 2
    assert "48;5;" not in block
    assert "Status" in lines[0]
    assert "kernel" in lines[1]


def test_full_preview_names_the_commands() -> None:
    text = render_action_preview("full", now=1_700_000_000)
    assert "pacman" in text
    assert "-Syu" in text
    assert "updates" in text
    assert "Full" in text


def test_share_report_preview_is_explicitly_non_mutating() -> None:
    item = action_catalog()["share"]
    assert item["mutates"] is False
    assert "sanitized" in item["why"]


def test_status_preview_names_the_snapshot() -> None:
    text = render_action_preview("status", now=1_700_000_000)
    assert "shows" in text
    assert "kernel" in text.lower() or "Snapshot" in text


def test_deck_book_preview_reads_a_ready_card(tmp_path: Path, monkeypatch) -> None:
    book_path = tmp_path / "deck-book.json"
    monkeypatch.setattr("arch_updater.preview.DECK_BOOK_PATH", book_path)
    monkeypatch.setattr("arch_updater.preview.DECK_BOOK_LOG_PATH", tmp_path / "deck-book.log")

    write_deck_book({"status": "ready status card\n"}, 2.5)

    assert load_deck_book_card("status") == "ready status card\n"
    assert render_deck_book_preview("status") == "ready status card\n"
    assert "cards=status" in (tmp_path / "deck-book.log").read_text(encoding="utf-8")


def test_attest_board_uses_lamps_not_a_log() -> None:
    text = render_attest_board(
        {
            "healthy": True,
            "reboot_required": False,
            "issues": [],
            "inventory": [{"mode": "PACNEW-PENDING", "message": "/etc/locale.gen.pacnew"}],
            "pending": {"repo": 12, "aur": 3},
        }
    )
    assert "Attest" in text
    assert "not a log" in text
    assert "12" in text
    assert "locale.gen.pacnew" in text
    assert "●" in text


def test_runway_reboot_boundary_wins_over_the_update_queue() -> None:
    text = render_runway(
        {
            "pacman_lock": False,
            "updates": {"repo": 13, "aur": 3},
            "root": {"free_bytes": 40 * 1024**3},
        },
        {"reboot_required": True},
    )
    assert "REBOOT FIRST" in text
    assert "13 repo" in text
    assert "3 AUR" in text
    assert "Attest" in text
    assert "READY TO REVIEW" not in text


def test_runway_lock_blocks_when_no_reboot_is_pending() -> None:
    text = render_runway(
        {"pacman_lock": True, "updates": {"repo": 0, "aur": 0}, "root": {}},
        {"reboot_required": False},
    )
    assert "BLOCKED" in text
    assert "Status" in text


def test_runway_ready_requires_attestation() -> None:
    snapshot = {"pacman_lock": False, "updates": {"repo": 2, "aur": 1}, "root": {}}
    attested = render_runway(
        snapshot,
        {"reboot_required": False, "healthy": True},
    )
    assert "READY TO REVIEW" in attested
    assert "healthy" in attested


def test_runway_missing_attestation_cannot_render_ready() -> None:
    snapshot = {"pacman_lock": False, "updates": {"repo": 2, "aur": 1}, "root": {}}
    missing = render_runway(snapshot, {})
    observed_only = render_runway(snapshot, {"reboot_required": False})
    unhealthy = render_runway(snapshot, {"reboot_required": False, "healthy": False})
    for text in (missing, observed_only, unhealthy):
        assert "READY TO REVIEW" not in text
        assert "READY" not in text
    assert "unattested" in missing
    assert "unattested" in observed_only
    board = render_attest_board({"reboot_required": False, "issues": [], "inventory": [], "pending": {}})
    assert "unknown" in board
    assert "clear" not in board


def test_marquee_moves() -> None:
    source = "repo linux 6.1-1 -> 6.2-1  ·  aur something 1-1 -> 2-1  ·  last OK full"
    left = marquee(source, width=24, speed=7.0, now=0)
    right = marquee(source, width=24, speed=7.0, now=3)
    assert left != right
    assert len(left) == 24
    assert len(right) == 24


def test_search_zen_is_packages_not_menu() -> None:
    blob = "\n".join(package_fzf_records("zen"))
    assert "linux-zen" in blob
    assert "kernel" in blob
    assert "\thelp\t" not in blob


def test_fzf_query_zen_replaces_menu_records() -> None:
    packages = fzf_records_for_query("zen")
    menu = fzf_records_for_query("")
    assert "pkg:kernel:linux-zen" in packages
    assert "\thelp\t" not in packages
    assert menu.startswith("help\t") or "\0help\t" in menu
    assert "pkg:kernel:linux-zen" not in menu
    zen_at = packages.find("linux-zen")
    benzene_at = packages.find("benzene")
    if benzene_at != -1:
        assert zen_at < benzene_at


def test_storefront_names_sources_and_keeps_browse_separate_from_install(monkeypatch) -> None:
    text = render_storefront({"updates": {"repo": 4, "aur": 3}})
    assert "repo" in text
    assert "AUR" in text
    assert "separate checkout" in text
    monkeypatch.setattr(
        "arch_updater.preview.load_hud_cache",
        lambda: {
            "repo_packages": ["linux 6.1-1 -> 6.2-1"],
            "aur_packages": ["yay 12-1 -> 12-2"],
        },
    )
    records = storefront_fzf_records("")
    assert "_sep_store_repo" in records
    assert "_sep_store_aur" in records
    assert "Official repo" in records
    assert "aur.archlinux.org" in records
    assert "pkg:repo:linux" in records
    assert "pkg:aur:yay" in records
    assert "pkg:kernel:linux-zen" in storefront_fzf_records("zen")
    assert "not a trust signal" in render_store_source("aur", {"updates": {"aur": 3}})
    assert "official catalog" in render_store_source("repo", {"updates": {"repo": 4}})


def test_package_name_rank_prefers_kernel_over_substring_noise() -> None:
    assert _name_rank("linux-zen", "zen") < _name_rank("benzene", "zen")


def test_package_search_never_invokes_yay(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run_capture(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(output="extra/linux-zen 7.2.2.zen1-1\n    The Linux Zen kernel\n")

    monkeypatch.setattr("arch_updater.preview.run_capture", fake_run_capture)
    monkeypatch.setattr(
        "arch_updater.preview.load_hud_cache",
        lambda: {"repo_packages": ["linux-zen 1 -> 2"], "aur_packages": ["zen-example-bin 1 -> 2"]},
    )
    blob = "\n".join(package_fzf_records("zen"))
    assert "linux-zen" in blob
    assert calls
    assert all(argv and argv[0] != "yay" and "yay" not in argv for argv in calls)


def test_package_records_mark_repo_and_aur_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        "arch_updater.preview.load_hud_cache",
        lambda: {
            "repo_packages": ["linux-zen 1 -> 2"],
            "aur_packages": ["zen-example-bin 1 -> 2"],
        },
    )
    monkeypatch.setattr("arch_updater.preview.run_capture", lambda *_args, **_kwargs: SimpleNamespace(output=""))
    blob = "\n".join(package_fzf_records("zen"))
    repo_line = next(line for line in blob.splitlines() if "pkg:repo:linux-zen" in line)
    aur_line = next(line for line in blob.splitlines() if "pkg:aur:zen-example-bin" in line)
    assert "38;5;110" in repo_line
    assert "38;5;180" in aur_line


def test_preview_pick_aur_does_not_shell_out_to_yay(monkeypatch, capsys) -> None:
    from arch_updater.main import main as app_main

    monkeypatch.setattr(
        "arch_updater.preview.load_hud_cache",
        lambda: {"aur_packages": ["foo 1 -> 2"]},
    )
    calls: list[list[str]] = []

    def fake_run_capture(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(output="")

    monkeypatch.setattr("arch_updater.main.run_capture", fake_run_capture)
    assert app_main(["preview-pick", "pkg:aur:foo"]) == 0
    assert calls == []
    assert "foo" in capsys.readouterr().out


def test_aur_hover_info_uses_cache_not_yay(monkeypatch) -> None:
    monkeypatch.setattr(
        "arch_updater.preview.load_hud_cache",
        lambda: {"aur_packages": ["zen-example-bin 1.0-1 -> 1.1-1"]},
    )
    text = aur_hover_info("zen-example-bin")
    assert "zen-example-bin" in text
    assert "1.0-1 -> 1.1-1" in text
    assert "yay" not in text


def test_search_multiword_intersects_package_terms(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.preview.load_hud_cache", lambda: {})

    def fake_run_capture(argv, **_kwargs):
        query = argv[-1]
        return SimpleNamespace(
            output=("extra/linux-zen 7.2.2.zen1-1\n    The Linux Zen kernel\n" if query in {"linux", "zen"} else "")
        )

    monkeypatch.setattr("arch_updater.preview.run_capture", fake_run_capture)
    blob = "\n".join(package_fzf_records("linux zen"))
    assert "linux-zen" in blob


def test_package_preview_is_view_only() -> None:
    text = render_package_preview(
        "aur",
        "yay",
        "Name            : yay\nVersion         : 12\nDescription     : yet another yogurt\nInstalled Size  : 2.00 MiB\nDepends On      : pacman git",
    )
    assert "yay -Si" in text
    assert "yet another yogurt" in text
    assert "2.00 MiB" in text
    assert "pacman git" in text
    assert "installed by Repos / AUR / Full" in text


def test_package_preview_keeps_repo_aur_cache_boundaries() -> None:
    si = "Name            : sample\nVersion         : 1\nDescription     : pkg\n"
    aur = render_package_preview("aur", "yay", si.replace("sample", "yay"))
    repo = render_package_preview("repo", "linux", si.replace("sample", "linux"))
    cache = render_package_preview("cache", "linux", si.replace("sample", "linux"))
    assert "AUR" in aur
    assert "community PKGBUILD" in aur
    assert "not a trust signal" in aur
    assert "official catalog" not in aur
    assert "local cache" not in aur
    assert "official catalog" in repo
    assert "not a trust signal" not in repo
    assert "local cache" in cache
    assert "/var/cache/pacman/pkg" in cache
    assert "official catalog" not in cache
    assert "not a trust signal" not in cache
    assert "pkg · aur · yay" in aur
    assert "pkg · repo · linux" in repo
    assert "pkg · cache · linux" in cache


def test_arch_news_parses_item_titles(monkeypatch) -> None:
    class FakeResponse:
        def read(self, _n: int) -> bytes:
            return (
                b"<rss><channel>"
                b"<item><title>linux 7.2</title></item>"
                b"<item><title>openssl advisory</title></item>"
                b"</channel></rss>"
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    from arch_updater.preview import fetch_arch_news

    assert fetch_arch_news() == ["linux 7.2", "openssl advisory"]


def test_parse_si_keeps_url_colons() -> None:
    fields = parse_si(
        "Name            : linux-zen\n"
        "URL             : https://github.com/zen-kernel/zen-kernel\n"
        "Depends On      : coreutils\n"
        "                  linux-firmware\n"
    )
    assert fields["Name"] == "linux-zen"
    assert fields["URL"] == "https://github.com/zen-kernel/zen-kernel"
    assert "linux-firmware" in fields["Depends On"]


def test_live_pending_subtitle_after_status_counts() -> None:
    cache = {
        "repo_packages": ["linux 1 -> 2"] * 12,
        "aur_packages": ["yay 1 -> 2"] * 3,
        "kernel": "linux-zen",
    }
    assert _live_subtitle("pending", "repo and AUR update queue", cache) == "12 repo · 3 AUR"
    assert "12 repo" in _live_subtitle("status", "kernel, disk, GPU, queue, ABI", cache)
    assert _live_subtitle("pending", "repo and AUR update queue", {}) == "repo and AUR update queue"


def test_header_only_queue_is_unknown_not_zero(tmp_path: Path, monkeypatch) -> None:
    header = {
        "hud": "arch-update 0.15.0   36.6 GiB   0 repo   0 AUR   no lock",
        "repo": 0,
        "aur": 0,
    }
    label = _live_subtitle("pending", "repo and AUR update queue", header)
    assert UNKNOWN_SOURCE in label
    assert "0 repo" not in label
    assert "0 AUR" not in label
    assert queue_counts_from_records(header) == (None, None)
    chips = render_chip_hud({"updates": {"repo": 0, "aur": 0}, "root": {}}, None)
    assert UNKNOWN_SOURCE in chips
    assert "0 repo" not in chips
    monkeypatch.setattr("arch_updater.preview.HUD_CACHE_PATH", tmp_path / "hud-cache.json")
    stored = write_hud_cache(
        {"updates": {"repo": 0, "aur": 0}, "kernel": {}, "pacman_lock": False, "pacnew": []},
        None,
        header["hud"],
    )
    stored_label = _live_subtitle("pending", "repo and AUR update queue", stored)
    assert UNKNOWN_SOURCE in stored_label
    assert "0 repo" not in stored_label


def test_hud_cache_keeps_pending_when_fast_snapshot(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "hud-cache.json"
    monkeypatch.setattr("arch_updater.preview.HUD_CACHE_PATH", cache_path)
    probed = {
        "updates": {
            "repo": 4,
            "aur": 1,
            "repo_packages": ["linux 1 -> 2", "bash 1 -> 2", "glibc 1 -> 2", "pacman 1 -> 2"],
            "aur_packages": ["yay 1 -> 2"],
        },
        "kernel": {"running_package": "linux-zen"},
        "pacman_lock": False,
        "pacnew": [],
    }
    write_hud_cache(probed, None, "hud")
    fast = {
        "updates": {"repo": None, "aur": None, "repo_packages": [], "aur_packages": []},
        "kernel": {"running_package": "linux-zen"},
        "pacman_lock": False,
        "pacnew": [],
    }
    kept = write_hud_cache(fast, None, "hud")
    assert kept["repo"] == 4
    assert kept["aur"] == 1
    blob = "\n".join(menu_fzf_records())
    assert "4 repo" in blob
    assert "1 AUR" in blob


def _visible(text: str) -> str:
    return ANSI_RE.sub("", text)


def test_status_badge_names_the_live_source_field() -> None:
    aur = render_status_badge({"source": "aur"}, "source")
    repo = render_status_badge({"source": "repo"}, "source")
    shown = render_status_badge({"mutates": False}, "mutates")
    runway = render_runway(
        {"pacman_lock": False, "updates": {"repo": 2, "aur": 1}, "root": {}},
        {"reboot_required": False, "healthy": True},
    )
    package = render_package_preview(
        "aur",
        "yay",
        "Name            : yay\nVersion         : 12\nDescription     : yet another yogurt\n",
    )
    assert "AUR" in _visible(aur)
    assert "source" in _visible(aur)
    assert "repo" in _visible(repo)
    assert "source" in _visible(repo)
    assert "shows" in _visible(shown)
    assert "mutates" in _visible(shown)
    assert "READY TO REVIEW" in _visible(runway)
    assert "reboot_required" in _visible(runway)
    assert "pacman_lock" in _visible(runway)
    assert "AUR · source" in _visible(package)
    assert "shows · mutates" in _visible(package)


def test_missing_source_badge_is_unknown_not_green_or_inferred() -> None:
    badge = render_status_badge({"package": "yay"}, "source")
    mutates = render_status_badge({"title": "Status"}, "mutates")
    package = render_package_preview("", "yay", "Name            : yay\nDescription     : hover\n")
    runway = render_runway({"updates": {"repo": 2, "aur": 1}, "root": {}}, {})
    assert UNKNOWN_SOURCE in _visible(badge)
    assert "source" in _visible(badge)
    assert GREEN not in badge
    assert "AUR" not in _visible(badge)
    assert "repo" not in _visible(badge)
    assert "READY" not in _visible(badge)
    assert UNKNOWN_SOURCE in _visible(mutates)
    assert "shows" not in _visible(mutates)
    assert GREEN not in mutates
    assert "unknown · source" in _visible(package)
    assert "pkg · unknown · yay" in _visible(package)
    assert "AUR · source" not in _visible(package)
    assert "repo · source" not in _visible(package)
    assert "READY" not in _visible(package)
    assert UNKNOWN_SOURCE in _visible(runway)
    assert "READY TO REVIEW" not in _visible(runway)
    assert "READY" not in _visible(runway)
    assert GREEN not in runway
