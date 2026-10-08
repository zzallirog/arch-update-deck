from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .engine import (
    STATE_ROOT,
    VAULT_PATH,
    attest,
    available_kernel_profiles,
    build_plan,
    classify_failures,
    detect_kernel,
    load_json,
    run_capture,
    run_update,
    select_kernel,
    snapshot,
)
from . import day
from .auto import init_config, run_auto, status, sudoers_rules
from .cve import scan_cves
from .scheduler import AUTO_UNIT, PATROL_UNIT, auto_status, remove_timer, install_daily_patrol_timer, install_weekly_auto_timer, last_patrol_report, run_patrol, scheduler_status
from .share_report import sanitize_patrol_report
from .transcripts import write_session_report
from .preview import aur_hover_info, fzf_records_for_query, render_action_preview, render_deck_book_preview, render_package_preview, storefront_fzf_records
from .ui import run_menu


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _print_status(data: dict[str, Any]) -> None:
    from .ui import render_status_page

    print(render_status_page(data), end="")


def _confirm_kernel(profile: str) -> bool:
    """Installing a kernel and rewriting GRUB needs a yes from a person at a terminal, or --yes."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            f"arch-update: kernel {profile} installs a package and rewrites GRUB; "
            "no terminal to ask on, pass --yes to confirm",
            file=sys.stderr,
        )
        return False
    from .ui import confirm

    return confirm(f"Install kernel profile {profile} and set it as the GRUB default?", affirmative="Install", negative="Cancel")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arch-update", description="Arch Update Deck: safe, supervised Arch Linux updates")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="Inspect live update and kernel state")
    status_parser.add_argument("--json", action="store_true")

    plan_parser = subparsers.add_parser("plan", help="Render the exact next mutation boundary")
    plan_parser.add_argument("--mode", choices=["full", "repo", "aur", "attest"], default="full")
    plan_parser.add_argument("--kernel", default="auto")

    run_parser = subparsers.add_parser("run", help="Run one update routine")
    run_parser.add_argument("--mode", choices=["full", "repo", "aur", "attest"], default="full")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--yes", action="store_true", help="Pass --noconfirm to package managers")

    subparsers.add_parser("attest", help="Verify current system and reboot boundary")
    kernel_parser = subparsers.add_parser("kernel", help="List or select a kernel profile")
    kernel_parser.add_argument("profile", nargs="?")
    kernel_parser.add_argument("--dry-run", action="store_true")
    kernel_parser.add_argument("--yes", action="store_true", help="Install and rewrite GRUB without asking")

    vault_parser = subparsers.add_parser("vault", help="Show or classify against the failure vault")
    vault_parser.add_argument("--classify")

    scan_parser = subparsers.add_parser("scan-sessions", help="Find update sessions in raw transcripts")
    scan_parser.add_argument("--output-dir", type=Path, default=STATE_ROOT / "reports")
    scan_parser.add_argument("--json", action="store_true")

    subparsers.add_parser("cve", help="Scan installed packages against Arch security tracker")
    schedule_parser = subparsers.add_parser("schedule", help="Show, install or remove the daily patrol and weekly update timers")
    schedule_parser.add_argument("--install", action="store_true", help="Enable the daily read-only patrol")
    schedule_parser.add_argument("--remove", action="store_true", help="Remove the daily patrol timer")
    schedule_parser.add_argument("--remove-auto", action="store_true", help="Remove the weekly unattended update timer")
    schedule_parser.add_argument("--install-auto", action="store_true", help="Enable the weekly unattended update")
    auto_parser = subparsers.add_parser("auto", help="Unattended update: look, update, ask the census about any failure, look again")
    auto_parser.add_argument("--dry-run", action="store_true", help="Look at the machine and change nothing")
    auto_parser.add_argument("--status", action="store_true", help="What still hangs from the last run, each with its fix")
    auto_parser.add_argument("--init", action="store_true", help="Write the starter config if there is none")
    auto_parser.add_argument("--ask", action="store_true", help="Show what is waiting and ask before updating")
    auto_parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    auto_parser.add_argument("--sudoers", action="store_true", help="Print the sudoers rules an unattended run needs")
    auto_parser.add_argument("--aur", action="store_true", help="With --sudoers: add the rules unattended AUR needs (a path to root for this account)")
    subparsers.add_parser("patrol", help="Read-only check: record queue, health and CVE findings (what the daily timer runs)")
    subparsers.add_parser("share-report", help="Render a sanitized local patrol report for manual sharing")

    subparsers.add_parser("menu", help="Open the looping terminal menu")
    preview_parser = subparsers.add_parser("preview", help="Internal: text for the menu preview pane")
    preview_parser.add_argument("target")
    preview_pkg = subparsers.add_parser("preview-pkg", help="Internal: package card for the menu preview pane")
    preview_pkg.add_argument("source")
    preview_pkg.add_argument("package")
    snap_parser = subparsers.add_parser("snapshot-json", help="Internal: machine snapshot for the menu")
    snap_parser.add_argument("--slow", action="store_true")
    search_parser = subparsers.add_parser("search", help="Internal: package search records for the menu")
    search_parser.add_argument("query", nargs="*")
    store_search_parser = subparsers.add_parser("store-search", help="Internal: store search records for the menu")
    store_search_parser.add_argument("query", nargs="*")
    pick_parser = subparsers.add_parser("preview-pick", help="Internal: preview of one menu pick")
    pick_parser.add_argument("target")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "menu"
    try:
        if command == "menu":
            return run_menu()
        if command == "status":
            data = snapshot(include_updates=True, include_slow=True)
            _json(data) if args.json else _print_status(data)
            return 0
        if command == "plan":
            _json(build_plan(args.mode, args.kernel))
            return 0
        if command == "run":
            report = run_update(args.mode, dry_run=args.dry_run, yes=args.yes)
            _json(report)
            return 1 if report["failed"] else 0
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
                return day.ask() if args.ask else day.scheduled()
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
        if command == "preview":
            print(render_action_preview(args.target), end="")
            return 0
        if command == "preview-pkg":
            if args.source.strip().lower() == "aur":
                info = run_capture(["yay", "-Si", "--", args.package], timeout=20).output
            else:
                info = run_capture(["pacman", "-Si", "--", args.package], timeout=15).output
            print(render_package_preview(args.source, args.package, info), end="")
            return 0
        if command == "snapshot-json":
            data = snapshot(include_updates=True, include_slow=bool(args.slow))
            json.dump(data, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
            return 0
        if command == "search":
            query = " ".join(args.query)
            sys.stdout.write(fzf_records_for_query(query))
            return 0
        if command == "store-search":
            query = " ".join(args.query)
            sys.stdout.write(storefront_fzf_records(query))
            return 0
        if command == "preview-pick":
            target = args.target
            if target.startswith("pkg:"):
                _, source, name = target.split(":", 2)
                if source == "aur":
                    info = aur_hover_info(name)
                else:
                    info = run_capture(["pacman", "-Si", "--", name], timeout=15).output
                print(render_package_preview(source, name, info), end="")
                return 0
            if target in {"", "_"}:
                return 0
            print(render_deck_book_preview(target), end="")
            return 0
    except (RuntimeError, ValueError) as exc:
        print(f"arch-update: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
