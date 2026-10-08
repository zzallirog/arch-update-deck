from __future__ import annotations

import argparse
import json
import os
import pwd
import signal
import sys
from pathlib import Path
from typing import Any

from . import __version__, auto, day
from .auto import init_config, run_auto, status, sudoers_rules
from .cve import scan_cves
from .engine import STATE_ROOT, VAULT_PATH, attest, available_kernel_profiles, classify_failures, default_kernel_package, detect_kernel, load_json, select_kernel, snapshot
from .scheduler import AUTO_UNIT, PATROL_UNIT, auto_status, remove_timer, install_daily_patrol_timer, install_weekly_auto_timer, last_patrol_report, run_patrol, scheduler_status
from .share_report import sanitize_patrol_report
from .textsafe import clean
from .transcripts import write_session_report


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _count(updates: dict[str, Any], store: str) -> str:
    """A queue length, or ? when it could not be read; the note beside it says why."""
    count = updates.get(store)
    return "?" if count is None else str(count)


def render_status(state: dict[str, Any]) -> str:
    updates, kernel, hypr = state.get("updates") or {}, state["kernel"], state.get("hyprland") or {}
    os_info = state.get("os", {})
    missing = hypr.get("missing_libraries") or []
    lines = [
        f"Arch Update Deck {__version__}",
        f"OS: {os_info.get('PRETTY_NAME') or os_info.get('NAME') or '?'}",
        f"Updates: repos {_count(updates, 'repo')} · AUR {_count(updates, 'aur')}",
        *(f"  {note}" for note in (updates.get("notes") or {}).values()),
        f"Kernel running: {kernel.get('running_package') or 'unknown'} · {kernel['running_release']} · modules "
        f"{'present' if kernel['running_modules_present'] else 'missing until reboot'}",
        f"Bootloader: {kernel.get('bootloader')} · default: {kernel.get('configured_default') or 'unknown'}",
        f"Default package: {default_kernel_package(kernel) or 'unknown'}",
        f"Root free: {state['root']['free_bytes'] / 1024**3:.1f} GiB",
        f"Pacman lock: {'yes' if state['pacman_lock'] else 'no'}",
        f"GPU: {state.get('gpu') or 'n/a'}",
        "Installed kernels:",
    ]
    for item in kernel.get("installed") or []:
        flags = [
            *(["running"] if item["package"] == kernel.get("running_package") else []),
            *(["default"] if item["package"] == default_kernel_package(kernel) else []),
            "vmlinuz" if item.get("vmlinuz_exists") else "NO vmlinuz",
            "initramfs" if item.get("initramfs_exists") else "NO initramfs",
            f"headers {item.get('headers') or 'missing'}",
        ]
        lines.append(f"  {item['package']} {item.get('version') or '?'} · " + ", ".join(flags))
    failed = state["failed_units"]
    lines += [
        f"Failed units: system {len(failed['system'])} · user {len(failed['user'])}",
        f"Pacnew/pacsave: {len(state.get('pacnew') or [])}",
        f"Hyprland ABI: {'broken' if missing else 'linked' if hypr.get('installed') else 'not installed'}",
        *(f"  {line}" for line in missing),
        *(["Hyprland foreign packages:", *(f"  {line}" for line in hypr["foreign_chain"])] if hypr.get("foreign_chain") else []),
    ]
    return clean("\n".join(lines) + "\n")


def _confirm_kernel(profile: str) -> bool:
    """Installing a kernel and rewriting GRUB needs a yes from a person at a terminal, or --yes."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            f"arch-update: kernel {profile} installs a package and rewrites GRUB; "
            "no terminal to ask on, pass --yes to confirm",
            file=sys.stderr,
        )
        return False
    return day.confirm(f"Install kernel profile {profile} and set it as the GRUB default?")


def _open_day() -> int:
    """No command: Update Day, whatever the config state. A machine with no config gets the starter one first."""
    notice = ""
    if not auto.CONFIG_PATH.is_file():
        try:
            notice = f"no config yet: {init_config().strip()}"
        except OSError as exc:
            notice = f"no config yet, and the starter config could not be written: {exc}"
    return day.ask(notice=notice)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arch-update",
        description="Arch Update Deck. With no command it opens Update Day: what is waiting, one question, then the update.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="What is installed, waiting and wrong on this machine")
    status_parser.add_argument("--json", action="store_true")
    kernel_parser = subparsers.add_parser("kernel", help="List the kernel profiles, or install one and make it the GRUB default")
    kernel_parser.add_argument("profile", nargs="?")
    kernel_parser.add_argument("--dry-run", action="store_true")
    kernel_parser.add_argument("--yes", action="store_true", help="Install and rewrite GRUB without asking")
    subparsers.add_parser("attest", help="Check the system and say whether a reboot is needed")
    auto_parser = subparsers.add_parser("auto", help="The update without questions: look, update, deal with any failure, look again")
    auto_parser.add_argument("--dry-run", action="store_true", help="Look at the machine and change nothing")
    auto_parser.add_argument("--status", action="store_true", help="What still hangs from the last run, each with its fix")
    auto_parser.add_argument("--init", action="store_true", help="Write the starter config if there is none")
    auto_parser.add_argument("--ask", action="store_true", help="Open Update Day and ask even when nothing is waiting")
    auto_parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    auto_parser.add_argument("--record", action="store_true", help=argparse.SUPPRESS)  # the weekly window: its answer goes to ask-result.json
    auto_parser.add_argument("--sudoers", action="store_true", help="Print the sudoers rules an unattended run needs")
    auto_parser.add_argument("--aur", action="store_true", help="With --sudoers: add the rules unattended AUR needs (a path to root for this account)")
    schedule_parser = subparsers.add_parser("schedule", help="Show, install or remove the daily patrol and the weekly update timers")
    schedule_parser.add_argument("--install", action="store_true", help="Enable the daily read-only patrol")
    schedule_parser.add_argument("--remove", action="store_true", help="Remove the daily patrol timer")
    schedule_parser.add_argument("--remove-auto", action="store_true", help="Remove the weekly unattended update timer")
    schedule_parser.add_argument("--install-auto", action="store_true", help="Enable the weekly unattended update")
    subparsers.add_parser("patrol", help="Read-only check: record queue, health and CVE findings (what the daily timer runs)")
    subparsers.add_parser("share-report", help="Print a sanitized copy of the last patrol report, safe to share")
    vault_parser = subparsers.add_parser("vault", help="Show the known failures, or name the ones in a piece of output")
    vault_parser.add_argument("--classify")
    subparsers.add_parser("cve", help="Scan installed packages against the Arch security tracker")
    scan_parser = subparsers.add_parser("scan-sessions", help="Find update sessions in raw transcripts")
    scan_parser.add_argument("--output-dir", type=Path, default=STATE_ROOT / "reports")
    scan_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command
    try:
        if command is None:
            return _open_day()
        if command == "status":
            data = snapshot(include_updates=True, include_slow=True)
            _json(data) if args.json else print(render_status(data), end="")
            return 0
        if command == "attest":
            _json(attest())
            return 0
        if command == "kernel":
            if not args.profile:
                _json({"detected": detect_kernel(), "profiles": available_kernel_profiles()})
                return 0
            if not args.dry_run and args.profile != "auto" and not args.yes and not _confirm_kernel(args.profile):
                return 1
            _json(select_kernel(args.profile, dry_run=args.dry_run))
            return 0
        if command == "vault":
            _json(classify_failures(args.classify) if args.classify else load_json(VAULT_PATH))
            return 0
        if command == "scan-sessions":
            result = write_session_report(args.output_dir)
            if args.json:
                _json(result["report"])
            else:
                print(Path(result["markdown"]).read_text(encoding="utf-8"), end="")
                print(f"Proven sessions: {result['report']['counts']['sessions_with_proven_changes']}")
            return 0
        if command == "cve":
            _json(scan_cves())
            return 0
        if command == "schedule":
            if args.install_auto:
                installed = install_weekly_auto_timer()
                _json(installed)
                return 0 if installed["installed"] else 1
            elif args.install:
                installed = install_daily_patrol_timer()
                _json(installed)
                return 0 if installed["installed"] else 1
            elif args.remove or args.remove_auto:
                removed = remove_timer(AUTO_UNIT if args.remove_auto else PATROL_UNIT)
                _json(removed)
                return 0 if removed["removed"] else 1
            else:
                _json({**scheduler_status(), "auto": auto_status()})
            return 0
        if command == "auto":
            if args.status or args.init:
                print(status() if args.status else init_config(), end="")
                return 0
            if args.ask or args.scheduled:
                return day.ask(always=True, record=args.record) if args.ask else day.scheduled()
            if args.sudoers:
                print(sudoers_rules(pwd.getpwuid(os.getuid()).pw_name, aur=args.aur), end="")
                return 0
            report = run_auto(dry_run=args.dry_run)
            _json(report)
            if args.dry_run:
                print(f"arch-update: dry run changed nothing; its report is in {report['wrote']}", file=sys.stderr)
                if any(verdict["case"] == "NO-ROOT" for verdict in report["verdicts"]):
                    print("arch-update: NO-ROOT is expected until the sudoers rule is installed (arch-update auto --sudoers)", file=sys.stderr)
            return report["exit_code"]
        if command == "patrol":
            _json(run_patrol())
            return 0
        if command == "share-report":
            report = last_patrol_report()
            if report is None:
                raise RuntimeError("no local patrol report yet; run arch-update patrol first")
            _json(sanitize_patrol_report(report))
            return 0
    except KeyboardInterrupt as stop:
        print()
        return 128 + getattr(stop, "signum", signal.SIGINT)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"arch-update: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
