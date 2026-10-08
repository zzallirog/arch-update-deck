from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time
from types import SimpleNamespace

import pytest

from arch_updater.ui import (
    BOOK_REFRESH_IDS,
    MENU_ACTIONS,
    MUTATING_IDS,
    PROBE_ERROR_RECOVERY,
    _GUM_CHOOSE_STYLE,
    _item_records,
    _kernel_menu,
    choose,
    confirm,
    menu_fzf_argv,
    refresh_deck_book,
    render_plan,
    render_probe_error,
    render_schedule_page,
    run_menu,
    selectable_actions,
    show_card,
)


def test_first_selectable_action_is_help() -> None:
    assert selectable_actions()[0][0] not in MUTATING_IDS
    assert selectable_actions()[0][0] == "help"
    assert {"engine", "full"} <= MUTATING_IDS


def test_fzf_restores_cursor_to_selected_action(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.ui._which", lambda name: "/usr/bin/fzf" if name == "fzf" else None)

    argv = menu_fzf_argv("Deck", selected_id="status")

    assert argv is not None
    assert "--track" in argv
    assert argv[argv.index("--id-nth") + 1] == "1"
    bind_values = [argv[index + 1] for index, value in enumerate(argv) if value == "--bind"]
    assert "load:clear-query+pos(5)" in bind_values


def test_runway_menu_action_stays_read_only(monkeypatch) -> None:
    state = {"pacman_lock": False, "updates": {"repo": 13, "aur": 3}, "root": {}}
    choices = iter(["runway", None])
    refreshes: list[bool] = []
    selected_ids: list[str | None] = []

    def choose_same_selection(*_args, **kwargs):
        selected_ids.append(kwargs.get("selected_id"))
        return next(choices)

    opened: list[str] = []
    monkeypatch.setattr("arch_updater.ui.refresh_deck_book", lambda: refreshes.append(True) or state)
    monkeypatch.setattr("arch_updater.ui._print_hud", lambda _state: None)
    monkeypatch.setattr("arch_updater.ui.choose", choose_same_selection)
    monkeypatch.setattr("arch_updater.ui.open_chapter", lambda choice: opened.append(choice))

    assert run_menu() == 0
    assert len(refreshes) == 1
    assert selected_ids == ["help", "runway"]
    assert opened == ["runway"]


def test_refresh_deck_book_prepares_right_pane_cards(monkeypatch) -> None:
    state = {
        "updates": {"repo": 13, "aur": 3},
        "pacman_lock": False,
        "root": {"free_bytes": 40 * 1024**3},
        "kernel": {"running_release": "6.12", "running_modules_present": True, "installed": []},
        "failed_units": {"system": [], "user": []},
        "pacnew": [],
        "hyprland": {},
        "os": {},
        "gpu": None,
    }
    written: dict[str, object] = {}

    monkeypatch.setattr("arch_updater.ui.snapshot_maybe_spin", lambda *_args: state)
    monkeypatch.setattr("arch_updater.ui.attest", lambda **_kwargs: {"healthy": True, "reboot_required": False, "issues": [], "inventory": [], "pending": {"repo": 13, "aur": 3}})
    monkeypatch.setattr("arch_updater.ui.scheduler_status", lambda: {"patrol": {}, "manager": "running", "cadence": "daily"})
    monkeypatch.setattr(
        "arch_updater.ui.available_kernel_profiles",
        lambda: [{"id": "zen", "label": "Arch Zen", "package": "linux-zen", "detected": False, "installed": True}],
    )
    monkeypatch.setattr("arch_updater.ui.write_deck_book", lambda cards, duration: written.update(cards=cards, duration=duration))

    assert refresh_deck_book() == state
    cards = written["cards"]
    assert isinstance(cards, dict)
    assert {"help", "engine", "status", "plan", "runway", "attest", "schedule", "kernel", "pending", "last", "log"} <= set(cards)
    assert "snapshot on entry" in cards["status"]
    menu_ids = {item_id for item_id, _t, _s in MENU_ACTIONS if not item_id.startswith("_sep") and item_id != "exit"}
    assert menu_ids <= set(cards)
    assert "Enter opens the profile choice" in cards["kernel"] or "profiles" in cards["kernel"]


def _book_text(cards: dict[str, object]) -> str:
    return "\n".join(str(value) for value in cards.values())


def test_refresh_deck_book_snapshot_oserror_is_visible_not_empty_queue(monkeypatch) -> None:
    written: dict[str, object] = {}

    def boom(*_args, **_kwargs):
        raise OSError("snapshot fd died")

    monkeypatch.setattr("arch_updater.ui.snapshot_maybe_spin", boom)
    monkeypatch.setattr("arch_updater.ui.scheduler_status", lambda: {"patrol": {}, "manager": "running", "cadence": "daily"})
    monkeypatch.setattr("arch_updater.ui.write_deck_book", lambda cards, duration: written.update(cards=cards, duration=duration))

    state = refresh_deck_book()
    cards = written["cards"]
    assert isinstance(cards, dict)
    blob = _book_text(cards)
    assert "OSError" in blob
    assert "snapshot fd died" in blob
    assert "probe: snapshot" in blob
    assert PROBE_ERROR_RECOVERY in blob
    assert "queue is empty" not in blob
    assert "no packages" not in blob
    assert "READY TO REVIEW" not in blob
    for chapter in ("status", "plan", "runway", "engine", "attest"):
        assert "OSError" in cards[chapter]
        assert "snapshot" in cards[chapter]
    assert state.get("updates", {}).get("repo") is None
    assert state.get("updates", {}).get("aur") is None


def test_refresh_deck_book_attest_oserror_stays_in_attest_chapter(monkeypatch) -> None:
    state = {
        "updates": {"repo": 13, "aur": 3},
        "pacman_lock": False,
        "root": {"free_bytes": 40 * 1024**3},
        "kernel": {"running_release": "6.12", "running_modules_present": True, "installed": []},
        "failed_units": {"system": [], "user": []},
        "pacnew": [],
        "hyprland": {},
        "os": {},
        "gpu": None,
    }
    written: dict[str, object] = {}

    def boom(**_kwargs):
        raise OSError("attest pipe closed")

    monkeypatch.setattr("arch_updater.ui.snapshot_maybe_spin", lambda *_args: state)
    monkeypatch.setattr("arch_updater.ui.attest", boom)
    monkeypatch.setattr("arch_updater.ui.scheduler_status", lambda: {"patrol": {}, "manager": "running", "cadence": "daily"})
    monkeypatch.setattr("arch_updater.ui.write_deck_book", lambda cards, duration: written.update(cards=cards, duration=duration))

    assert refresh_deck_book() == state
    cards = written["cards"]
    blob = _book_text(cards)
    assert "OSError" in cards["attest"]
    assert "probe: attest" in cards["attest"]
    assert "attest pipe closed" in cards["attest"]
    assert PROBE_ERROR_RECOVERY in cards["attest"]
    assert "queue is empty" not in blob
    assert "READY TO REVIEW" not in cards["runway"]
    assert "snapshot on entry" in cards["status"]
    assert "13" in cards["status"]


def test_render_probe_error_names_the_probe() -> None:
    text = render_probe_error("snapshot", OSError("snapshot fd died"))
    assert "probe: snapshot" in text
    assert "OSError: snapshot fd died" in text
    assert PROBE_ERROR_RECOVERY in text
    assert "queue is empty" not in text


def _forbidden_binaries(argv: list[object]) -> set[str]:
    names = set()
    for part in argv:
        if not isinstance(part, str):
            continue
        base = part.rsplit("/", 1)[-1].lower()
        if base in {"pacman", "yay", "sudo", "reboot"}:
            names.add(base)
        if part.lower() in {"run_update", "_apply_mode"}:
            names.add(part.lower())
    return names


def test_mocked_refresh_records_no_pacman_yay_sudo_or_reboot(monkeypatch) -> None:
    state = {
        "updates": {"repo": 13, "aur": 3},
        "pacman_lock": False,
        "root": {"free_bytes": 40 * 1024**3},
        "kernel": {"running_release": "6.12", "running_modules_present": True, "installed": []},
        "failed_units": {"system": [], "user": []},
        "pacnew": [],
        "hyprland": {},
        "os": {},
        "gpu": None,
    }
    recorded: list[list[object]] = []
    written: dict[str, object] = {}

    def capture(argv, **_kwargs):
        recorded.append(list(argv))
        return SimpleNamespace(returncode=0, output="", stdout="")

    def subprocess_run(argv, **_kwargs):
        recorded.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("arch_updater.ui.snapshot_maybe_spin", lambda *_args: state)
    monkeypatch.setattr(
        "arch_updater.ui.attest",
        lambda **_kwargs: {
            "healthy": True,
            "reboot_required": False,
            "issues": [],
            "inventory": [],
            "pending": {"repo": 13, "aur": 3},
        },
    )
    monkeypatch.setattr("arch_updater.ui.scheduler_status", lambda: {"patrol": {}, "manager": "running", "cadence": "daily"})
    monkeypatch.setattr("arch_updater.ui.write_deck_book", lambda cards, duration: written.update(cards=cards, duration=duration))
    monkeypatch.setattr("arch_updater.ui.run_capture", capture)
    monkeypatch.setattr("arch_updater.ui.subprocess.run", subprocess_run)
    monkeypatch.setattr("arch_updater.ui.run_update", lambda **_kwargs: recorded.append(["run_update"]) or {"failed": False})
    monkeypatch.setattr("arch_updater.ui._apply_mode", lambda mode, current: recorded.append(["_apply_mode", mode]) or current)

    assert refresh_deck_book() == state
    assert refresh_deck_book() == state
    assert "cards" in written
    for argv in recorded:
        assert not _forbidden_binaries(argv), argv
    assert BOOK_REFRESH_IDS.isdisjoint(MUTATING_IDS)


def test_plan_enter_opens_chapter_not_silent_refresh(monkeypatch) -> None:
    state = {"pacman_lock": False, "updates": {}, "root": {}}
    opened: list[str] = []
    choices = iter(["plan", None])
    monkeypatch.setattr("arch_updater.ui.refresh_deck_book", lambda: state)
    monkeypatch.setattr("arch_updater.ui._print_hud", lambda _state: None)
    monkeypatch.setattr("arch_updater.ui.choose", lambda *_a, **_k: next(choices))
    monkeypatch.setattr("arch_updater.ui.open_chapter", lambda choice: opened.append(choice))
    assert run_menu() == 0
    assert opened == ["plan"]


def test_kernel_is_a_mutating_menu_slot() -> None:
    assert "kernel" in {item_id for item_id, _title, _blurb in MENU_ACTIONS}
    assert "kernel" in MUTATING_IDS


def test_kernel_chooser_does_not_reload_main_search() -> None:
    argv = menu_fzf_argv("Desired kernel", preview=None, search_command=None)
    assert argv is not None
    assert "--disabled" not in argv
    joined = " ".join(argv)
    assert "reload(" not in joined
    assert "search" not in joined
    records = _item_records([("zen", "Arch Zen", "linux-zen"), ("auto", "Keep detected", "")])
    assert "zen\taction\t" in records
    assert "auto\taction\t" in records


def test_kernel_menu_lists_profiles_and_returns(monkeypatch) -> None:
    seen: list[object] = []

    def fake_choose(items, header, **kwargs):
        seen.append({"items": list(items), "header": header, "search_command": kwargs.get("search_command"), "preview": kwargs.get("preview")})
        return None

    monkeypatch.setattr(
        "arch_updater.ui.available_kernel_profiles",
        lambda: [
            {"id": "auto", "label": "Keep detected kernel", "package": None, "headers": None, "detected": True, "installed": True},
            {"id": "zen", "label": "Arch Zen", "package": "linux-zen", "headers": "linux-zen-headers", "detected": False, "installed": True},
        ],
    )
    monkeypatch.setattr("arch_updater.ui.choose", fake_choose)
    state = {"kernel": {"installed": [{"package": "linux-zen", "vmlinuz_exists": True, "initramfs_exists": True}]}}
    assert _kernel_menu(state) is state
    assert seen
    assert seen[0]["search_command"] is None
    assert {row[0] for row in seen[0]["items"]} == {"auto", "zen"}
    assert "kernel" in seen[0]["header"].lower()


def test_plan_explains_default_and_non_automatic_schedule() -> None:
    text = render_plan(
        {
            "mode": "full",
            "commands": [["sudo", "pacman", "-Syu"], ["yay", "-Sua"]],
            "mutation_boundary": "No reboot.",
            "preflight": {
                "pacman_lock": False,
                "root_free_bytes": 40 * 1024**3,
                "pending_updates": {"repo": 3, "aur": 1},
                "reboot_required": True,
            },
        }
    )

    assert "Default: Updater Engine / Full" in text
    assert "Schedule — reads the queue, health and CVE daily" in text
    assert "Engine will run:" in text
    assert "Reboot / Attest first" in text


def test_engine_routes_to_the_existing_full_confirmation_path(monkeypatch) -> None:
    state = {"pacman_lock": False, "updates": {}, "root": {}}
    choices = iter(["engine", None])
    applied: list[str] = []

    monkeypatch.setattr("arch_updater.ui.refresh_deck_book", lambda: state)
    monkeypatch.setattr("arch_updater.ui._print_hud", lambda _state: None)
    monkeypatch.setattr("arch_updater.ui.choose", lambda *_args, **_kwargs: next(choices))
    monkeypatch.setattr("arch_updater.ui._apply_mode", lambda mode, current: applied.append(mode) or current)

    assert run_menu() == 0
    assert applied == ["full"]


def test_schedule_renders_compact_patrol_evidence() -> None:
    text = render_schedule_page(
        {
            "manager": "running",
            "cadence": "daily",
            "patrol": {"installed": True, "active": True},
            "last_patrol": {
                "finished_at": "now",
                "pending": {"repo": 13, "aur": 3},
                "healthy": True,
                "reboot_required": True,
                "cve_available": True,
                "cve_findings": 21,
            },
        }
    )
    assert "Last queue: repos 13 · AUR 3" in text
    assert "Last CVE: 21 findings" in text


def test_share_menu_shows_a_sanitized_preview(monkeypatch) -> None:
    choices = iter(["share", None])
    cards: list[tuple[str, str | None]] = []
    report = {
        "finished_at": "now",
        "hostname": "private-host",
        "snapshot": {"updates": {"repo": 1, "aur": 2}},
        "verification": {"healthy": True, "reboot_required": False, "issues": []},
        "cve": {"available": True, "findings": [{"package": "secret-package"}]},
    }

    monkeypatch.setattr("arch_updater.ui.refresh_deck_book", lambda: {})
    monkeypatch.setattr("arch_updater.ui._print_hud", lambda _state: None)
    monkeypatch.setattr("arch_updater.ui.choose", lambda *_args, **_kwargs: next(choices))
    monkeypatch.setattr("arch_updater.ui.last_patrol_report", lambda: report)
    monkeypatch.setattr("arch_updater.ui.show_card", lambda text, title=None: cards.append((text, title)))

    assert run_menu() == 0
    assert cards[0][1] == "Share report · preview only"
    assert "arch-update-share/v1" in cards[0][0]
    assert "private-host" not in cards[0][0]
    assert "secret-package" not in cards[0][0]


def test_fallback_enter_cancels(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.ui._which", lambda _name: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert choose([("status", "Status"), ("full", "Full (repos + AUR)")], "Choose") is None


def test_fallback_number_selects(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.ui._which", lambda _name: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    assert choose([("_sep_inspect", "── inspect ──"), ("status", "Status"), ("full", "Full")], "Choose") == "status"


def test_confirm_enter_is_no(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.ui._which", lambda _name: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert confirm("Apply full update?") is False


def test_gum_choose_defaults_to_status(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Status\n")

    monkeypatch.setattr("arch_updater.ui._which", lambda name: "/usr/bin/gum" if name == "gum" else None)
    monkeypatch.setattr("arch_updater.ui._interactive", lambda: True)
    monkeypatch.setattr("arch_updater.ui.subprocess.run", fake_run)

    assert choose(MENU_ACTIONS, "Choose routine", selected_id="status") == "status"
    assert captured["argv"][captured["argv"].index("--selected") + 1] == "Status"
    assert captured["argv"][captured["argv"].index("--selected") + 1] != "Full"


def test_card_does_not_dump_to_tty(monkeypatch, capsys) -> None:
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("arch_updater.ui._which", lambda name: "/usr/bin/gum" if name == "gum" else None)
    monkeypatch.setattr("arch_updater.ui._interactive", lambda: True)
    monkeypatch.setattr("arch_updater.ui.subprocess.run", fake_run)
    show_card("hello wall of text")
    assert "hello wall of text" not in capsys.readouterr().out


def test_cards_do_not_force_a_terminal_background(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("arch_updater.ui._which", lambda name: "/usr/bin/gum" if name == "gum" else None)
    monkeypatch.setattr("arch_updater.ui._interactive", lambda: True)
    monkeypatch.setattr("arch_updater.ui.subprocess.run", fake_run)
    show_card("transparent")

    assert captured["argv"][captured["argv"].index("--background") + 1] == "#0c1118"
    assert captured["argv"][captured["argv"].index("--border-foreground") + 1] == "110"


def test_fzf_preview_uses_action_id(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="status\n")

    monkeypatch.setattr(
        "arch_updater.ui._which",
        lambda name: f"/usr/bin/{name}" if name in {"fzf", "gum"} else None,
    )
    monkeypatch.setattr("arch_updater.ui._interactive", lambda: True)
    monkeypatch.setattr("arch_updater.ui.subprocess.run", fake_run)
    assert choose(MENU_ACTIONS, "hover", selected_id="status") == "status"
    assert captured["argv"][0] == "/usr/bin/fzf"
    assert "--read0" in captured["argv"]
    assert "--disabled" in captured["argv"]
    joined = " ".join(captured["argv"])
    assert "start:clear-query" in joined
    assert "load:clear-query+pos(5)" in joined
    assert "change:reload(" in joined
    assert "sleep 0.15" in joined
    assert "{q}" in joined
    assert "search" in joined
    assert "yay" not in joined
    assert captured["argv"][captured["argv"].index("--with-shell") + 1] == "/bin/sh -c"
    preview = captured["argv"][captured["argv"].index("--preview") + 1]
    assert "preview-pick" in preview
    assert "{1}" in preview
    assert captured["argv"][captured["argv"].index("--with-nth") + 1] == "3"


def test_theme_stays_cold_no_raspberry_or_slot_fill(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.ui._which", lambda name: f"/usr/bin/{name}" if name == "fzf" else None)
    assert "212" not in _GUM_CHOOSE_STYLE
    assert _GUM_CHOOSE_STYLE[_GUM_CHOOSE_STYLE.index("--cursor.foreground") + 1] == "110"
    argv = menu_fzf_argv("hover")
    assert argv is not None
    color = argv[argv.index("--color") + 1]
    assert "48;5;" not in color
    assert "#07090d" in color
    assert "#0c1118" in color
    assert "#1e3144" in color
    joined = " ".join(argv)
    assert "212" not in joined
    assert "#ff" not in color.lower()


def test_live_fzf_types_zen_and_accepts_linux_zen() -> None:
    if not shutil.which("fzf"):
        pytest.skip("fzf is not installed")
    argv = menu_fzf_argv("menu · type zen", preview=None)
    assert argv is not None
    assert "--disabled" in argv
    assert "--filter" not in argv
    env = os.environ.copy()
    env["FZF_DEFAULT_OPTS"] = ""
    env["FZF_DEFAULT_COMMAND"] = ""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    def drain(seconds: float) -> bytes:
        data = b""
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
            if not ready:
                break
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data

    screen = drain(1.2)
    for byte in b"zen":
        os.write(fd, bytes([byte]))
        time.sleep(0.04)
    screen += drain(1.6)
    os.write(fd, b"\r")
    deadline = time.time() + 4
    status = None
    while time.time() < deadline:
        waited, child_status = os.waitpid(pid, os.WNOHANG)
        screen += drain(0.1)
        if waited:
            status = child_status
            break
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise AssertionError("fzf did not accept after typing zen")
    assert status == 0
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen.decode("utf-8", "replace"))
    assert "linux-zen" in text
    assert "kernel" in text
    accepted = [line.strip() for line in text.splitlines() if line.strip().startswith("pkg:")]
    assert accepted
    assert accepted[-1] == "pkg:kernel:linux-zen"


def test_live_storefront_types_zen_and_accepts_linux_zen() -> None:
    if not shutil.which("fzf"):
        pytest.skip("fzf is not installed")
    argv = menu_fzf_argv("STORE · type a package", preview=None, search_command="store-search")
    assert argv is not None
    env = os.environ.copy()
    env["FZF_DEFAULT_OPTS"] = ""
    env["FZF_DEFAULT_COMMAND"] = ""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    def drain(seconds: float) -> bytes:
        data = b""
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
            if not ready:
                break
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data

    screen = drain(1.2)
    for byte in b"zen":
        os.write(fd, bytes([byte]))
        time.sleep(0.04)
    screen += drain(1.6)
    os.write(fd, b"\r")
    deadline = time.time() + 4
    status = None
    while time.time() < deadline:
        waited, child_status = os.waitpid(pid, os.WNOHANG)
        screen += drain(0.1)
        if waited:
            status = child_status
            break
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise AssertionError("storefront fzf did not accept after typing zen")
    assert status == 0
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen.decode("utf-8", "replace"))
    assert "linux-zen" in text
    accepted = [line.strip() for line in text.splitlines() if line.strip().startswith("pkg:")]
    assert accepted[-1] == "pkg:kernel:linux-zen"


def test_gum_confirm_defaults_to_no(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr("arch_updater.ui._which", lambda name: "/usr/bin/gum" if name == "gum" else None)
    monkeypatch.setattr("arch_updater.ui._interactive", lambda: True)
    monkeypatch.setattr("arch_updater.ui.subprocess.run", fake_run)
    assert confirm("Apply?") is False
    assert "--default=false" in captured["argv"]
