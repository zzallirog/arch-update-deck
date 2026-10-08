from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .engine import (
    STATE_ROOT,
    VAULT_PATH,
    attest,
    available_kernel_profiles,
    default_kernel_package,
    detect_kernel,
    load_json,
    pacman_lock_report,
    planned_update_commands,
    privilege_command,
    run_capture,
    run_update,
    select_kernel,
    snapshot,
)
from .preview import (
    ACTION_SLOTS,
    STOREFRONT_SLOTS,
    marquee,
    paint,
    render_attest_board,
    render_book_page,
    render_chip_hud,
    render_help,
    render_runway,
    render_arch_news,
    render_orphans,
    render_package_preview,
    render_pacman_log,
    render_slot,
    load_deck_book_card,
    render_action_preview,
    render_store_source,
    render_storefront,
    write_deck_book,
    write_hud_cache,
    CYAN,
    PINK,
)
from .surface import fzf_color_spec
from .transcripts import write_session_report
from .cve import scan_cves
from .scheduler import install_daily_patrol_timer, last_patrol_report, scheduler_status
from .share_report import sanitize_patrol_report


MENU_ACTIONS: list[tuple[str, str, str]] = ACTION_SLOTS

MUTATING_IDS = frozenset({"engine", "repo", "aur", "full", "kernel"})
BOOK_REFRESH_IDS = frozenset({"runway", "status", "plan", "attest"})
CHAPTER_OPEN_IDS = frozenset(
    {
        "help",
        "runway",
        "status",
        "plan",
        "attest",
        "last",
        "log",
        "orphans",
        "news",
        "cve",
    }
)
_GUM_CHOOSE_STYLE = [
    "--height",
    "18",
    "--header.foreground",
    "245",
    "--cursor.foreground",
    "110",
    "--selected.foreground",
    "110",
]


def _parts(item: Sequence[str]) -> tuple[str, str, str]:
    if len(item) >= 3:
        return item[0], item[1], item[2]
    return item[0], item[1], ""


def selectable_actions() -> list[tuple[str, str, str]]:
    return [item for item in MENU_ACTIONS if not item[0].startswith("_sep")]


def is_separator(item_id: str) -> bool:
    return item_id.startswith("_sep")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _drain_stdin() -> None:
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            while True:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
        except BlockingIOError:
            pass
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except OSError:
        return


def _fzf_env() -> dict[str, str]:
    env = os.environ.copy()
    env["FZF_DEFAULT_OPTS"] = ""
    return env


def _run_fzf(
    argv: list[str],
    *,
    input_text: str | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    _drain_stdin()
    if "--bind" not in argv or "start:clear-query" not in " ".join(argv):
        argv = [*argv, "--bind", "start:clear-query"]
    if "load:clear-query" not in " ".join(argv):
        argv = [*argv, "--bind", "load:clear-query"]
    if "--with-shell" not in argv:
        argv = [*argv, "--with-shell", "/bin/sh -c"]
    return subprocess.run(
        argv,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        check=False,
        env=_fzf_env(),
    )


def fzf_menu_items(items: Sequence[Sequence[str]]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    skipped_leading_sep = False
    for item in items:
        item_id, title, subtitle = _parts(item)
        if not skipped_leading_sep and is_separator(item_id):
            skipped_leading_sep = True
            continue
        rows.append((item_id, title, subtitle))
    return rows


def _preview_command(*args: str) -> str:
    quoted = " ".join(shlex.quote(part) for part in args)
    root = str(Path(__file__).resolve().parent.parent)
    current = os.environ.get("PYTHONPATH", "")
    pythonpath = root if not current else f"{root}:{current}"
    return (
        f"env PYTHONPATH={shlex.quote(pythonpath)} "
        f"{shlex.quote(sys.executable)} -m arch_updater {quoted}"
    )


def menu_fzf_argv(
    header: str,
    preview: str | None = "action",
    search_command: str | None = "search",
    selected_id: str | None = None,
) -> list[str] | None:
    fzf = _which("fzf")
    if not fzf:
        return None
    selected_position: int | None = None
    if selected_id:
        menu_ids = [item_id for item_id, _, _ in fzf_menu_items(MENU_ACTIONS)]
        if selected_id in menu_ids:
            selected_position = menu_ids.index(selected_id) + 1
    argv = [
        fzf,
        "--ansi",
        "--read0",
        "--highlight-line",
        "--height",
        "82%",
        "--layout",
        "reverse",
        "--style",
        "minimal",
        "--no-separator",
        "--gap",
        "1",
        "--prompt",
        "› ",
        "--pointer",
        "›",
        "--info",
        "inline-right",
        "--cycle",
        "--delimiter",
        "\t",
        "--with-nth",
        "3",
        "--accept-nth",
        "1",
        "--header",
        header,
        "--bind",
        "ctrl-/:toggle-preview",
        "--with-shell",
        "/bin/sh -c",
        "--color",
        fzf_color_spec(),
    ]
    if search_command:
        search_cmd = _preview_command(search_command)
        argv.extend(
            [
                "--disabled",
                "--bind",
                f"change:reload(sleep 0.15; {search_cmd} {{q}})",
                "--bind",
                f"start:clear-query+reload({search_cmd})",
            ]
        )
    if selected_position is not None:
        argv.extend(["--track", "--id-nth", "1", "--bind", f"load:clear-query+pos({selected_position})"])
    if preview == "action":
        argv.extend(
            [
                "--preview",
                _preview_command("preview-pick", "{1}"),
                "--preview-window",
                "right:54%:wrap:noborder",
                "--preview-label",
                "",
            ]
        )
    elif preview:
        argv.extend(["--preview", preview, "--preview-window", "right:54%:wrap:border-sharp"])
    return argv


def _item_records(items: Sequence[Sequence[str]]) -> str:
    rows: list[str] = []
    for item_id, title, subtitle in fzf_menu_items(items):
        shown = render_slot(title, subtitle or "", mutates=item_id in MUTATING_IDS)
        rows.append(f"{item_id}\taction\t{shown}")
    return "\0".join(rows) + "\0"


def _choose_fzf(
    items: Sequence[Sequence[str]],
    header: str,
    selected_id: str | None = None,
    preview: str | None = "action",
    search_command: str | None = "search",
) -> str | None:
    argv = menu_fzf_argv(
        header,
        preview=preview,
        search_command=search_command,
        selected_id=selected_id,
    )
    if not argv:
        return None
    payload = "" if search_command else _item_records(items)
    result = _run_fzf(argv, input_text=payload)
    if result.returncode != 0:
        return None
    selected = result.stdout.strip().split("\n", 1)[0].strip()
    return selected or None


def choose(
    items: Sequence[Sequence[str]],
    header: str,
    selected_id: str | None = None,
    preview: str | None = "action",
    search_command: str | None = "search",
) -> str | None:
    unpacked = [_parts(item) for item in items]
    labels = [title for _, title, _ in unpacked]
    selected_label = next((title for item_id, title, _ in unpacked if item_id == selected_id), None)
    if _interactive() and _which("fzf"):
        picked = _choose_fzf(
            items,
            header,
            selected_id=selected_id,
            preview=preview,
            search_command=search_command,
        )
        return picked
    gum = _which("gum") if _interactive() else None
    if gum:
        argv = [gum, "choose", "--header", header]
        if selected_label:
            argv.extend(["--selected", selected_label])
        argv.extend(_GUM_CHOOSE_STYLE)
        argv.extend(labels)
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, check=False)
        if result.returncode != 0:
            return None
        selected = result.stdout.strip()
        return next((item_id for item_id, title, _ in unpacked if title == selected), None)
    print(header)
    index_map: list[str] = []
    for item_id, title, subtitle in unpacked:
        if is_separator(item_id):
            print(paint("2", f"  · {title} ·"))
            continue
        index_map.append(item_id)
        print(f"{len(index_map)}. {title}")
        if subtitle:
            print(f"   {paint('38;5;245', subtitle)}")
    while True:
        try:
            raw = input("Choice (Enter to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        try:
            selected_index = int(raw) - 1
        except ValueError:
            print(f"'{raw}' is not a number 1-{len(index_map)}, try again.")
            continue
        if 0 <= selected_index < len(index_map):
            return index_map[selected_index]
        print(f"Pick a number between 1 and {len(index_map)}, try again.")


def confirm(prompt: str, affirmative: str = "Yes, apply", negative: str = "Cancel") -> bool:
    gum = _which("gum") if _interactive() else None
    if gum:
        result = subprocess.run(
            [gum, "confirm", "--default=false", prompt,"--affirmative", affirmative, "--negative", negative],
            check=False,
        )
        return result.returncode == 0
    print(prompt)
    try:
        raw = input("Confirm [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return raw in {"y", "yes"}


def _wait_back() -> None:
    sys.stdout.write(paint("38;5;245", "\n  Enter — back\n"))
    sys.stdout.flush()
    if not sys.stdin.isatty():
        return
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (termios.error, OSError, EOFError):
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    _drain_stdin()


def show_card(text: str, title: str = "back") -> None:
    body = text if text.endswith("\n") else text + "\n"
    gum = _which("gum") if _interactive() else None
    if gum:
        subprocess.run(
            [
                gum,
                "style",
                "--border",
                "normal",
                "--border-foreground",
                "110",
                "--background",
                "#0c1118",
                "--foreground",
                "252",
                "--padding",
                "0 2",
                "--width",
                "82",
                "--margin",
                "1 0",
                body,
            ],
            check=False,
        )
        _wait_back()
        return
    print(body)
    _wait_back()


def show_scroll(text: str) -> None:
    body = text if text.endswith("\n") else text + "\n"
    fzf = _which("fzf") if _interactive() else None
    if fzf:
        _run_fzf(
            [
                fzf,
                "--ansi",
                "--disabled",
                "--no-sort",
                "--reverse",
                "--height",
                "90%",
                "--border",
                "sharp",
                "--prompt",
                "back › ",
                "--info",
                "inline",
                "--color",
                fzf_color_spec(),
            ],
            input_text=body,
            capture=False,
        )
        return
    gum = _which("gum") if _interactive() else None
    if gum:
        show_card(body)
        return
    path = STATE_ROOT / "last-view.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(f"(no pager) wrote {path}")


def filter_choice(
    labels: Sequence[str],
    header: str,
    placeholder: str = "type to filter",
    preview: str | None = None,
) -> str | None:
    if not labels:
        return None
    if _interactive() and _which("fzf"):
        argv = [
            _which("fzf") or "fzf",
            "--ansi",
            "--height",
            "82%",
            "--layout",
            "reverse",
            "--border",
            "rounded",
            "--border-label",
            " list ",
            "--prompt",
            "▸ ",
            "--pointer",
            "▶",
            "--info",
            "inline",
            "--header",
            header,
        ]
        if preview:
            argv.extend(["--preview", preview, "--preview-window", "right:56%:wrap:border-sharp", "--preview-label", " pkg "])
        result = _run_fzf(argv, input_text="\n".join(labels) + "\n")
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    gum = _which("gum") if _interactive() else None
    if gum:
        result = subprocess.run(
            [gum, "filter", "--header", header, "--placeholder", placeholder],
            input="\n".join(labels) + "\n",
            text=True,
            stdout=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            return None
        selected = result.stdout.strip()
        return selected or None
    return choose([(label, label) for label in labels], header)


def load_last_run() -> dict[str, Any] | None:
    path = STATE_ROOT / "last-run.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _count_text(updates: dict[str, Any], store: str) -> str:
    """A queue length, or ? when it could not be read; the note beside it says why."""
    count = updates.get(store)
    return "?" if count is None else str(count)


def render_status_page(state: dict[str, Any]) -> str:
    updates = state.get("updates") or {}
    kernel = state["kernel"]
    lines = [
        f"Arch Update Deck {__version__}",
        f"OS: {state.get('os', {}).get('PRETTY_NAME') or state.get('os', {}).get('NAME') or '?'}",
        f"Updates: repos {_count_text(updates, 'repo')} · AUR {_count_text(updates, 'aur')}",
        *(f"  {note}" for note in (updates.get("notes") or {}).values()),
        (
            f"Kernel running: {kernel.get('running_package') or 'unknown'} · "
            f"{kernel['running_release']} · modules "
            f"{'present' if kernel['running_modules_present'] else 'missing until reboot'}"
        ),
        f"Bootloader: {kernel.get('bootloader')} · default: {kernel.get('configured_default') or 'unknown'}",
        f"Default package: {default_kernel_package(kernel) or 'unknown'}",
        f"Root free: {state['root']['free_bytes'] / 1024**3:.1f} GiB",
        f"Pacman lock: {'yes' if state['pacman_lock'] else 'no'}",
        f"GPU: {state.get('gpu') or 'n/a'}",
    ]
    lines.append("Installed kernels:")
    for item in kernel.get("installed") or []:
        flags = []
        if item["package"] == kernel.get("running_package"):
            flags.append("running")
        if item["package"] == default_kernel_package(kernel):
            flags.append("default")
        flags.append("vmlinuz" if item.get("vmlinuz_exists") else "NO vmlinuz")
        flags.append("initramfs" if item.get("initramfs_exists") else "NO initramfs")
        flags.append(f"headers {item.get('headers') or 'missing'}")
        lines.append(f"  {item['package']} {item.get('version') or '?'} · " + ", ".join(flags))
    failed = state["failed_units"]
    lines.append(f"Failed units: system {len(failed['system'])} · user {len(failed['user'])}")
    lines.append(f"Pacnew/pacsave: {len(state.get('pacnew') or [])}")
    hypr = state.get("hyprland") or {}
    missing = hypr.get("missing_libraries") or []
    lines.append(f"Hyprland ABI: {'broken' if missing else 'linked' if hypr.get('installed') else 'not installed'}")
    if missing:
        lines.extend(f"  {line}" for line in missing)
    if hypr.get("foreign_chain"):
        lines.append("Hyprland foreign packages:")
        lines.extend(f"  {line}" for line in hypr["foreign_chain"])
    return "\n".join(lines) + "\n"


def render_attest_page(result: dict[str, Any]) -> str:
    return render_attest_board(result)


def _plan_from_snapshot(state: dict[str, Any], verification: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "mode": "full",
        "commands": planned_update_commands("full"),
        "kernel_profile": None,
        "detected_kernel": state.get("kernel") or {},
        "preflight": {
            "pacman_lock": bool(state.get("pacman_lock")),
            "root_free_bytes": (state.get("root") or {}).get("free_bytes", 0),
            "pending_updates": state.get("updates") or {},
            "reboot_required": bool((verification or {}).get("reboot_required")),
        },
        "mutation_boundary": "Deck never reboots. Package commands stay behind Repos/AUR/Full confirmation.",
    }


PROBE_ERROR_RECOVERY = "Enter refreshes book · pacman and yay are not run"


def render_probe_error(probe: str, exc: BaseException) -> str:
    """Right-pane chapter for a failed probe. This is not an empty queue."""
    kind = type(exc).__name__
    body = "\n".join(
        [
            f"probe: {probe}",
            f"error: {kind}: {exc}",
            "",
            "this is not an empty queue",
            f"recovery: {PROBE_ERROR_RECOVERY}",
        ]
    )
    return render_book_page(probe, body, f"{probe} failed · {kind}")


def _call_probe(name: str, fn):
    try:
        return fn(), None
    except (OSError, RuntimeError, ValueError) as exc:
        return None, exc


def _empty_probe_state() -> dict[str, Any]:
    """Fail-closed HUD payload. Counts stay None so the book cannot claim an empty queue."""
    return {
        "updates": {"repo": None, "aur": None, "repo_packages": [], "aur_packages": []},
        "root": {},
        "kernel": {},
        "failed_units": {"system": [], "user": []},
        "pacnew": [],
        "hyprland": {},
        "os": {},
        "gpu": None,
    }


def render_kernel_chapter(state: dict[str, Any]) -> str:
    from .engine import CONFIG_PATH, load_json

    profiles = list(load_json(CONFIG_PATH).get("kernels") or [])
    installed = {item["package"]: item for item in (state.get("kernel") or {}).get("installed", [])}
    lines = [
        "Kernel · inspect-only until confirm",
        "Enter opens the profile choice. Esc returns to the deck.",
        "source local · no mutation",
        "",
    ]
    if not profiles:
        lines.append("profiles unavailable")
    for profile in profiles:
        package = profile.get("package") or "auto"
        marks = []
        if profile.get("detected"):
            marks.append("running")
        if profile.get("installed"):
            marks.append("installed")
        artifact = installed.get(profile.get("package") or "")
        if artifact:
            marks.append("vmlinuz" if artifact.get("vmlinuz_exists") else "NO vmlinuz")
        extra = f" · {' · '.join(marks)}" if marks else ""
        lines.append(f"{profile.get('id')}  {profile.get('label')}  {package}{extra}")
    return render_book_page("kernel", "\n".join(lines), "profiles · Enter = choose, not install")


def open_chapter(choice: str) -> None:
    """Focused right-pane card, then back to the Deck loop."""
    text = load_deck_book_card(choice) or render_action_preview(choice)
    show_card(text, title=choice)


def refresh_deck_book() -> dict[str, Any]:
    """Probe once before fzf starts, then let its right pane stay instant."""
    started = time.monotonic()
    state, snapshot_exc = _call_probe(
        "snapshot",
        lambda: snapshot_maybe_spin(True, True, "preparing Deck book · queue · health · CVE"),
    )
    if snapshot_exc is not None:
        state = _empty_probe_state()
        verification, attest_exc = None, None
    else:
        verification, attest_exc = _call_probe("attest", lambda: attest(after=state))
    schedule, schedule_exc = _call_probe("schedule", scheduler_status)

    snapshot_error = render_probe_error("snapshot", snapshot_exc) if snapshot_exc is not None else None
    attest_error = render_probe_error("attest", attest_exc) if attest_exc is not None else None

    if schedule_exc is not None:
        schedule_card = render_probe_error("schedule", schedule_exc)
    else:
        schedule_card = render_book_page("schedule", render_schedule_page(schedule or {}), "patrol · Enter refreshes book")

    if snapshot_error is not None:
        engine_card = snapshot_error
        plan_card = snapshot_error
        status_card = snapshot_error
        runway_card = snapshot_error
        attest_card = snapshot_error
    else:
        status_card = render_book_page("status", render_status_page(state), "snapshot on entry · Enter refreshes book")
        if attest_error is not None:
            engine_card = attest_error
            plan_card = attest_error
            runway_card = attest_error
            attest_card = attest_error
        else:
            engine_card = render_book_page(
                "Updater Engine",
                render_plan(_plan_from_snapshot(state, verification)),
                "default Full · Enter asks for confirmation before pacman/yay",
            )
            plan_card = render_book_page(
                "plan",
                render_plan(_plan_from_snapshot(state, verification)),
                "update defaults and boundaries · Enter refreshes book",
            )
            runway_card = render_runway(state, verification or {})
            attest_card = render_attest_page(verification or {})

    last = load_last_run()
    pending_lines = []
    updates = (state or {}).get("updates") or {}
    for line in list(updates.get("repo_packages") or [])[:12]:
        pending_lines.append(f"repo  {line}")
    for line in list(updates.get("aur_packages") or [])[:12]:
        pending_lines.append(f"AUR   {line}")
    pending_body = "\n".join(pending_lines) if pending_lines else "no package records in snapshot · inspect-only"
    pacnew = "\n".join(state.get("pacnew") or []) or "no pacnew"
    failed = state.get("failed_units") or {}
    units_body = "\n".join([*(failed.get("system") or []), *(failed.get("user") or [])]) or "no failed units"
    cards = {
        "help": render_help(),
        "engine": engine_card,
        "store": render_storefront(state or {}),
        "runway": runway_card,
        "status": status_card,
        "pending": render_book_page("pending", pending_body, "queue · Enter opens the list, not an install"),
        "plan": plan_card,
        "last": render_book_page("last", render_last_run(last) if last else "No last-run.json yet.", "previous run · Enter opens the chapter"),
        "log": render_book_page("log", "Enter opens the tail of /var/log/pacman.log.\nHover does not read the log.", "not loaded on hover"),
        "orphans": render_book_page("orphans", "Enter opens pacman -Qtd.\nHover does not run pacman.", "not loaded on hover"),
        "news": render_book_page("news", "Enter loads the archlinux.org/news headlines.\nNetwork only on an explicit Enter.", "not loaded on hover"),
        "cve": render_book_page("cve", "Enter opens arch-audit.\nHover does not scan.", "not loaded on hover"),
        "schedule": schedule_card,
        "repo": render_action_preview("repo"),
        "aur": render_action_preview("aur"),
        "full": render_action_preview("full"),
        "kernel": render_kernel_chapter(state or {}),
        "attest": attest_card,
        "pacnew": render_book_page("pacnew", pacnew, "diff · Enter opens the list"),
        "units": render_book_page("units", units_body, "failed units · Enter opens the list"),
        "vault": render_book_page("vault", "Enter opens the failure catalog.", "playbooks"),
        "share": render_action_preview("share"),
        "sessions": render_action_preview("sessions"),
        "exit": render_book_page("exit", "Enter closes the deck.", "no mutation"),
    }
    write_deck_book(cards, time.monotonic() - started)
    return state


def render_run_report(report: dict[str, Any]) -> str:
    status = "FAILED" if report["failed"] else "OK"
    lines = [f"[{status}] {report['mode']} update · run {report['run_id']}"]
    for command in report.get("commands") or []:
        argv = " ".join(command["argv"])
        outcome = "ok" if command["returncode"] == 0 else f"exit {command['returncode']}"
        lines.append(f"  {argv} -> {outcome}")
        for mode in command.get("failure_modes") or []:
            lines.append(f"    ! known failure mode: {mode}")
    verification = report.get("verification") or {}
    lines.append(
        f"Healthy: {verification.get('healthy')} · reboot required: {verification.get('reboot_required')}"
    )
    for issue in verification.get("issues") or []:
        lines.append(f"  - {issue['mode']}: {issue['message']}")
    for item in verification.get("inventory") or []:
        lines.append(f"  inventory {item['mode']}: {item['message']}")
    lines.append(f"Full log: {STATE_ROOT / 'runs' / report['run_id'] / 'run.log'}")
    return "\n".join(lines) + "\n"


def render_plan(plan: dict[str, Any]) -> str:
    pending = plan["preflight"]["pending_updates"]
    locked = bool(plan["preflight"]["pacman_lock"])
    reboot_required = bool(plan["preflight"].get("reboot_required"))
    root_free = plan["preflight"]["root_free_bytes"] / 1024**3
    queue_count = int(pending.get("repo", 0)) + int(pending.get("aur", 0))
    if reboot_required:
        default_now = "Now: Reboot / Attest first; Engine after the reboot."
    elif locked:
        default_now = "Now: Engine waits until the pacman lock is gone."
    elif queue_count:
        default_now = "Now: Engine is ready; Full can be run with confirmation."
    else:
        default_now = "Now: queue is empty; no reason to run Engine."
    lines = [
        "Decision map. Plan runs nothing.",
        "source local · no mutation",
        "Repos → AUR → Attest",
        "",
        "Default: Updater Engine / Full",
        "  When: there is a queue, no pacman lock and no reboot pending.",
        "  What: Repos → AUR → Attest. A separate confirmation comes before the run.",
        "",
        "Other moves:",
        "  Repos — signed Arch/CachyOS packages only; a quick safe update.",
        "  AUR — separately, when ready to review community PKGBUILD builds.",
        "  Runway — first, if unsure: reboot-first / blocked / ready.",
        "  Schedule — reads the queue, health and CVE daily; never updates.",
        "  Kernel — only by explicit profile choice; a reboot may follow.",
        "",
        "On the machine now:",
        f"  {default_now}",
        f"  Queue: {pending.get('repo', 0)} repo · {pending.get('aur', 0)} AUR",
        f"  pacman lock: {'present — Engine waits' if locked else 'none'}",
        f"  reboot: {'reboot first' if reboot_required else 'not required per the last Attest'}",
        f"  Free on /: {root_free:.1f} GiB",
        f"  Boundary: {plan['mutation_boundary']}",
        "",
        "Engine will run:",
    ]
    for command in plan["commands"]:
        lines.append("  " + " ".join(command))
    if plan.get("kernel_profile"):
        profile = plan["kernel_profile"]
        lines.append(f"Kernel profile {profile.get('id')}: {profile.get('label')}")
    return "\n".join(lines) + "\n"


def render_schedule_page(result: dict[str, Any]) -> str:
    from .surface import READ_ONLY_LINE, SCHEDULE_BOUNDARY, load_patrol_history

    patrol = result.get("patrol") or {}
    origin = "live now" if result.get("live") else "persisted historical report"
    lines = [
        "Deck autopilot",
        SCHEDULE_BOUNDARY,
        READ_ONLY_LINE,
        f"origin: {origin}",
        "Every day: package queue, system state and CVE → a fresh snapshot.",
        "Never in the background: installs, sudo or reboot. Reads package databases with pacman -Q, checkupdates and yay -Qua.",
        "",
        f"User manager: {result.get('manager', 'unknown')}",
        f"Cadence: {result.get('cadence', 'unknown')}",
        f"Patrol: {'active' if patrol.get('active') else 'not enabled'} · {patrol.get('unit', 'arch-update-patrol.timer')}",
    ]
    if patrol.get("next"):
        lines.append(f"Next: {patrol['next']}")
    if patrol.get("last"):
        lines.append(f"Timer last: {patrol['last']}")
    last = result.get("last_patrol") or {}
    if last.get("finished_at"):
        lines.append(f"Last report: {last['finished_at']}")
        pending = last.get("pending") or {}
        lines.append(f"Last queue: repos {pending.get('repo', '?')} · AUR {pending.get('aur', '?')}")
        if "healthy" in last:
            health = "clear" if last.get("healthy") else "needs attention"
            if last.get("reboot_required"):
                health += " · reboot boundary"
            lines.append(f"Last health: {health}")
        if last.get("cve_available") is True:
            lines.append(f"Last CVE: {last.get('cve_findings', 0)} findings")
        elif last.get("cve_error"):
            lines.append("Last CVE: scan failed; see patrol report")
    elif not patrol.get("installed"):
        lines.append("Not installed: choose Schedule again and confirm the daily check.")
    history = load_patrol_history()
    if history:
        lines.append("")
        lines.append("history (newest first, capped):")
        for item in history[:5]:
            pending = item.get("pending") or {}
            lines.append(
                f"  {item.get('finished_at')} · healthy={item.get('healthy')} · "
                f"reboot={item.get('reboot_required')} · "
                f"repo={pending.get('repo')} aur={pending.get('aur')} · "
                f"cve={item.get('cve_findings')}"
            )
    return "\n".join(lines) + "\n"


def render_last_run(report: dict[str, Any]) -> str:
    lines = [render_run_report(report).rstrip(), "", "Failure playbooks:"]
    vault = {mode["id"]: mode for mode in load_json(VAULT_PATH).get("modes", [])}
    seen: list[str] = []
    for command in report.get("commands") or []:
        for mode_id in command.get("failure_modes") or []:
            if mode_id in seen:
                continue
            seen.append(mode_id)
            mode = vault.get(mode_id)
            if not mode:
                lines.append(f"  {mode_id}: (unknown)")
                continue
            lines.append(f"  {mode_id}: {mode['meaning']}")
            lines.append(f"    fix: {mode['response']}")
    if not seen:
        lines.append("  none recorded")
    log_path = STATE_ROOT / "runs" / str(report.get("run_id", "")) / "run.log"
    lines.append("")
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = log_text[-8000:]
        lines.append(f"--- run.log tail ({log_path}) ---")
        lines.append(tail)
    else:
        lines.append(f"No run.log at {log_path}")
    return "\n".join(lines) + "\n"


def snapshot_maybe_spin(include_updates: bool, include_slow: bool, title: str) -> dict[str, Any]:
    if not include_updates and not include_slow:
        return snapshot(include_updates=False, include_slow=False)
    gum = _which("gum") if _interactive() else None
    if not gum:
        return snapshot(include_updates=include_updates, include_slow=include_slow)
    argv = [
        gum,
        "spin",
        "-s",
        "pulse",
        "--title",
        title or "queue · health · CVE · book ready",
        "--show-stdout",
        "--",
        sys.executable,
        "-m",
        "arch_updater",
        "snapshot-json",
    ]
    if include_slow:
        argv.append("--slow")
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode == 0 and result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            pass
    return snapshot(include_updates=include_updates, include_slow=include_slow)


def _print_hud(state: dict[str, Any]) -> None:
    last = load_last_run()
    hud = render_chip_hud(state, last)
    cache = write_hud_cache(state, last, hud)
    ticker = marquee(str(cache.get("ticker") or "open Status to list pending packages"), width=64)
    body = hud + "\n" + paint("38;5;245", ticker)
    print()
    gum = _which("gum") if _interactive() else None
    if gum:
        subprocess.run(
            [
                gum,
                "style",
                "--border",
                "hidden",
                "--border-foreground",
                "240",
                "--background",
                "#0c1118",
                "--foreground",
                "252",
                "--padding",
                "0 1",
                "--width",
                "82",
                body,
            ],
            check=False,
        )
    else:
        print(body)
    print()


def _apply_mode(mode: str, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("pacman_lock"):
        show_card(pacman_lock_report())
        return state
    commands = planned_update_commands(mode)
    from .surface import confirm_preflight_text, format_argv

    observed = {
        "pending": state.get("updates") or {},
        "pacman_lock": state.get("pacman_lock"),
        "reboot_required": (state.get("verification") or {}).get("reboot_required"),
    }
    argv = commands[0] if commands else [mode]
    preview = confirm_preflight_text(observed, argv)
    if len(commands) > 1:
        preview += "\n" + "\n".join(format_argv(command) for command in commands[1:])
    if not confirm(f"{mode}\n{preview}", affirmative="Update", negative="Cancel"):
        return state
    try:
        report = run_update(mode=mode, confirm_cleanup=confirm)
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
        if "pacman lock exists" in message:
            show_card(pacman_lock_report())
        else:
            show_card(message)
        return snapshot(include_updates=False, include_slow=False)
    show_card(render_run_report(report))
    return snapshot_maybe_spin(True, True, "attesting after apply")


def _kernel_menu(state: dict[str, Any]) -> dict[str, Any]:
    profiles = available_kernel_profiles()
    installed = {item["package"]: item for item in (state.get("kernel") or detect_kernel()).get("installed", [])}
    items: list[tuple[str, str, str]] = []
    for profile in profiles:
        package = profile.get("package")
        artifact = installed.get(package or "")
        marks = []
        if profile.get("detected"):
            marks.append("running")
        if profile.get("installed"):
            marks.append("installed")
        if artifact:
            marks.append("vmlinuz" if artifact.get("vmlinuz_exists") else "NO vmlinuz")
            marks.append("initramfs" if artifact.get("initramfs_exists") else "NO initramfs")
        suffix = f" · {' · '.join(marks)}" if marks else ""
        items.append((profile["id"], profile["label"], suffix.strip(" ·") or "kernel profile"))
    if not items:
        show_card("No kernel profiles available.\n", title="Kernel")
        return state
    profile_id = choose(items, "Desired kernel · Esc back to the deck", search_command=None, preview=None)
    if not profile_id:
        return state
    profile = next((item for item in profiles if item["id"] == profile_id), None)
    if profile is None:
        show_card(f"kernel: no profile named {profile_id}\n", title="Kernel")
        return state
    if profile_id == "auto":
        show_card("No change: keep detected kernel.\n")
        return state
    argv = " ".join([privilege_command(), "pacman", "-S", "--needed", profile["package"], profile.get("headers") or ""]).strip()
    if not confirm(
        f"Kernel {profile['package']}\n{argv}\nGRUB_DEFAULT from grub.cfg",
        affirmative="Install",
        negative="Cancel",
    ):
        return state
    try:
        result = select_kernel(profile_id)
    except (RuntimeError, ValueError) as exc:
        show_card(str(exc))
        return snapshot(include_updates=False, include_slow=False)
    lines = []
    if result.get("changed"):
        lines.append(f"Kernel set to {result.get('package')}")
        if result.get("bootloader_changed"):
            lines.append(f"GRUB updated (backup: {result.get('backup', 'n/a')})")
            if result.get("planned_default"):
                lines.append(f"GRUB_DEFAULT={result['planned_default']}")
        elif result.get("reason"):
            lines.append(result["reason"])
        if result.get("reboot_required"):
            lines.append("Reboot required to boot into this kernel. This tool will not reboot.")
    else:
        lines.append(f"No change: {result.get('reason', 'kernel unchanged')}")
    show_card("\n".join(lines) + "\n")
    return snapshot(include_updates=False, include_slow=False)


def _pending_menu(state: dict[str, Any]) -> dict[str, Any]:
    updates = state.get("updates") or {}
    if updates.get("repo") is None:
        state = snapshot_maybe_spin(True, False, "probing repos · AUR")
        updates = state["updates"]
    repo = list(updates.get("repo_packages") or [])
    aur = list(updates.get("aur_packages") or [])
    if not repo and not aur:
        show_card("No pending repo or AUR packages.\n")
        return state
    if _interactive() and _which("fzf"):
        payload: list[str] = []
        for line in repo:
            name = line.split()[0]
            payload.append(f"repo\t{name}\t{paint(CYAN, 'repo')}  {line}")
        for line in aur:
            name = line.split()[0]
            payload.append(f"aur\t{name}\t{paint(PINK, 'AUR ')}  {line}")
        while True:
            argv = [
                _which("fzf") or "fzf",
                "--ansi",
                "--height",
                "82%",
                "--layout",
                "reverse",
                "--border",
                "sharp",
                "--border-label",
                " queue ",
                "--prompt",
                "package ▸ ",
                "--pointer",
                "▶",
                "--info",
                "inline",
                "--delimiter",
                "\t",
                "--with-nth",
                "3",
                "--accept-nth",
                "1,2",
                "--header",
                f"{len(repo)} official · {len(aur)} AUR · hover = description · Enter = details · Esc = back",
                "--preview",
                _preview_command("preview-pkg", "{1}", "{2}"),
                "--preview-window",
                "right:56%:wrap:border-sharp",
                "--preview-label",
                " pkg ",
            ]
            result = _run_fzf(argv, input_text="\n".join(payload) + "\n")
            if result.returncode != 0 or not result.stdout.strip():
                return state
            source, _, name = result.stdout.strip().partition("\t")
            name = name.strip()
            if source.strip() == "aur":
                info = run_capture(["yay", "-Si", "--", name], timeout=60).output
            else:
                info = run_capture(["pacman", "-Si", "--", name], timeout=30).output
            show_scroll(info or f"no info for {name}\n")
        return state
    labels = [f"repo  {line}" for line in repo] + [f"aur   {line}" for line in aur]
    selected = filter_choice(labels, f"Pending packages ({len(repo)} repo, {len(aur)} AUR)")
    if not selected:
        return state
    source, _, rest = selected.partition("  ")
    name = rest.split()[0] if rest.strip() else ""
    if not name:
        return state
    if source.strip() == "aur":
        info = run_capture(["yay", "-Si", "--", name], timeout=60).output
    else:
        info = run_capture(["pacman", "-Si", "--", name], timeout=30).output
    show_scroll(info or f"no info for {name}\n")
    return state


def _show_package(source: str, name: str) -> None:
    if source == "aur":
        info = run_capture(["yay", "-Si", "--", name], timeout=60).output
    else:
        info = run_capture(["pacman", "-Si", "--", name], timeout=30).output
    show_card(render_package_preview(source, name, info), title=name)


def _storefront_menu(state: dict[str, Any]) -> dict[str, Any]:
    state = snapshot_maybe_spin(True, False, "opening repo + AUR storefront")
    while True:
        choice = choose(
            STOREFRONT_SLOTS,
            "STORE · type a package name to search · the card does not install",
            preview="action",
            search_command="store-search",
        )
        if choice in {None, "store:back", "_"}:
            return state
        if choice.startswith("pkg:"):
            _, source, name = choice.split(":", 2)
            _show_package(source, name)
            continue
        if choice == "store:pending":
            state = _pending_menu(state)
            continue
        if choice in {"store:repo", "store:aur"}:
            show_card(render_store_source(choice.removeprefix("store:"), state), title="Store")
            continue
        return state


def _pacnew_menu(state: dict[str, Any]) -> dict[str, Any]:
    files = list(state.get("pacnew") or [])
    if not files:
        state = snapshot(include_updates=False, include_slow=False)
        files = list(state.get("pacnew") or [])
    if not files:
        show_card("No pacnew/pacsave files.\n")
        return state
    selected = filter_choice(
        files,
        "New stock configs next to yours. Enter = diff. Overwrites nothing.",
    )
    if not selected:
        return state
    original = selected.removesuffix(".pacnew").removesuffix(".pacsave")
    diff = run_capture(["diff", "-u", "--", original, selected], timeout=30)
    body = diff.output or f"diff exit {diff.returncode} (identical or missing)\n"
    show_scroll(body)
    return state


def _units_menu(state: dict[str, Any]) -> dict[str, Any]:
    failed = state.get("failed_units") or {"system": [], "user": []}
    labels = [f"system {name}" for name in failed.get("system") or []] + [
        f"user   {name}" for name in failed.get("user") or []
    ]
    if not labels:
        show_card("No failed units in the current snapshot.\n")
        return state
    selected = filter_choice(labels, "Failed units")
    if not selected:
        return state
    scope, _, unit = selected.partition(" ")
    argv = ["systemctl", "--no-pager", "-n", "40", "status", "--", unit.strip()]
    if scope.strip() == "user":
        argv.insert(1, "--user")
    show_scroll(run_capture(argv, timeout=20).output or f"no status for {unit}\n")
    return state


def _vault_menu() -> None:
    modes = load_json(VAULT_PATH).get("modes") or []
    labels = [f"{mode['id']}  {mode['meaning']}" for mode in modes]
    selected = filter_choice(labels, "Failure vault")
    if not selected:
        return
    mode_id = selected.split()[0]
    mode = next((item for item in modes if item["id"] == mode_id), None)
    if not mode:
        return
    show_card(
        "\n".join(
            [
                mode["id"],
                f"stage: {mode.get('stage')}",
                f"automatic: {mode.get('automatic')}",
                "",
                mode.get("meaning", ""),
                "",
                f"fix: {mode.get('response', '')}",
                "",
                f"match: {mode.get('match')}",
            ]
        )
        + "\n"
    )


def run_menu() -> int:
    state = refresh_deck_book()
    selected_id = "help"
    while True:
        _print_hud(state)
        choice = choose(
            MENU_ACTIONS,
            "menu · ready book on the right · Enter refreshes the card · Repos/AUR/Full update",
            selected_id=selected_id,
        )
        if choice in {None, "exit"}:
            return 0
        if not choice or choice.startswith("_"):
            continue
        if is_separator(choice):
            continue
        selected_id = choice
        try:
            if choice.startswith("pkg:"):
                _, source, name = choice.split(":", 2)
                _show_package(source, name)
                continue
            if choice in CHAPTER_OPEN_IDS:
                if choice == "last":
                    report = load_last_run()
                    show_card(render_last_run(report) if report else "No last-run.json yet.\n", title="Last run")
                elif choice == "log":
                    show_scroll(render_pacman_log())
                elif choice == "orphans":
                    show_card(render_orphans(), title="Orphans")
                elif choice == "news":
                    show_card(render_arch_news(), title="News")
                elif choice == "cve":
                    result = scan_cves()
                    lines = [f"available: {result.get('available')}", f"findings: {len(result.get('findings') or [])}"]
                    if result.get("error"):
                        lines.append(f"error: {result['error']}")
                    for finding in (result.get("findings") or [])[:12]:
                        lines.append(f"{finding.get('severity', '?')} · {', '.join(finding.get('packages') or [])} · {', '.join(finding.get('issues') or [])}")
                    show_scroll("\n".join(lines) + "\n")
                else:
                    open_chapter(choice)
            elif choice == "store":
                state = _storefront_menu(state)
            elif choice == "pending":
                state = _pending_menu(state)
            elif choice == "schedule":
                result = scheduler_status()
                if result.get("available") and not (result.get("patrol") or {}).get("installed"):
                    if confirm(
                        "Enable the daily read-only patrol at 09:30 (+ up to 15 minutes)?\n"
                        "It checks the queue, health and CVE; it installs no packages.",
                        affirmative="Enable check",
                        negative="Just show for now",
                    ):
                        installed = install_daily_patrol_timer()
                        if not installed.get("installed"):
                            show_card(
                                "Daily patrol was not enabled.\n\n"
                                + (installed.get("output") or "systemctl returned no detail")
                                + "\n",
                                title="Schedule",
                            )
                        result = scheduler_status()
                state = refresh_deck_book()
                open_chapter("schedule")
            elif choice in {"engine", "repo", "aur", "full"}:
                state = _apply_mode("full" if choice == "engine" else choice, state)
            elif choice == "kernel":
                state = _kernel_menu(state)
            elif choice == "pacnew":
                state = _pacnew_menu(state)
            elif choice == "units":
                state = _units_menu(state)
            elif choice == "vault":
                _vault_menu()
            elif choice == "share":
                report = last_patrol_report()
                if report is None:
                    show_card("No local patrol report yet. Run the read-only patrol first.\n", title="Share report")
                else:
                    from .surface import render_share_preview as _share_preview

                    sanitized = sanitize_patrol_report(report)
                    show_card(
                        _share_preview(sanitized) + json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
                        title="Share report · preview only",
                    )
            elif choice == "sessions":
                if confirm(
                    "Parse Codex/Claude logs for package installs?",
                    affirmative="Scan",
                    negative="Cancel",
                ):
                    result = write_session_report(STATE_ROOT / "reports")
                    markdown = Path(result["markdown"]).read_text(encoding="utf-8")
                    show_scroll(
                        markdown
                        + f"\nProven sessions: {result['report']['counts']['sessions_with_proven_changes']}\n"
                    )
            else:
                show_card(f"no handler for {choice}\n", title=choice)
        except (RuntimeError, ValueError) as exc:
            show_card(f"arch-update: {exc}\n")
        except KeyboardInterrupt:
            print()
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(run_menu())
