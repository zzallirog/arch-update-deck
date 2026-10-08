"""Shared helpers for the Deck's interactive surface: palette, card layout, patrol history."""

from __future__ import annotations

import json
import shlex
from typing import Any

from .engine import STATE_ROOT

READ_ONLY_LINE = "source local · no mutation"
SCHEDULE_BOUNDARY = "read-only patrol · never installs"
HISTORY_PATH = STATE_ROOT / "patrol-history.json"
HISTORY_CAP = 12

TRUECOLOR_HEX = {
    "canvas": "#07090d",
    "underlay": "#0c1118",
    "raised": "#16202c",
    "focus": "#1e3144",
    "ink": "#d7e8f4",
    "muted": "#6a8196",
    "info": "#7ec4e8",
    "success": "#6db8a4",
    "warning": "#d4a45a",
    "danger": "#c4886a",
    "aur": "#c9a06e",
    "repo": "#7eb8d4",
    "specular": "#a8d4ee",
}


def fzf_color_spec() -> str:
    """The fzf colour spec: canvas, underlay, raised focus."""
    c = TRUECOLOR_HEX
    return (
        f"bg:{c['canvas']},bg+:{c['focus']},fg:{c['ink']},fg+:{c['ink']},"
        f"hl:{c['specular']},hl+:{c['warning']},pointer:{c['specular']},"
        f"prompt:{c['info']},header:{c['muted']},info:{c['muted']},"
        f"gutter:{c['canvas']},preview-bg:{c['underlay']},border:{c['info']},"
        f"separator:{c['raised']},label:{c['muted']},query:{c['ink']}"
    )


def format_argv(argv: list[str] | tuple[str, ...]) -> str:
    """Fish-safe displayed command: quoted tokens, no unquoted expansion."""
    return " ".join(shlex.quote(str(part)) for part in argv)


































def confirm_preflight_text(observed: dict[str, Any], argv: list[str]) -> str:
    pending = observed.get("pending") or observed.get("updates") or {}
    lock = observed.get("pacman_lock")
    reboot = observed.get("reboot_required")
    lines = [
        "observed:",
        f"  queue repo={pending.get('repo')} aur={pending.get('aur')}",
        f"  pacman_lock={lock}",
        f"  reboot_required={reboot}",
        "",
        "mutate:",
        f"  {format_argv(argv)}",
    ]
    return "\n".join(lines)




def parse_queue_versions(line: str) -> tuple[str, str] | None:
    if "->" not in line:
        return None
    left, right = line.split("->", 1)
    parts = left.split()
    if len(parts) < 2:
        return None
    return parts[-1], right.strip().split()[0]


def render_share_preview(sanitized: dict[str, Any]) -> str:
    summary = sanitized.get("summary") or {}
    lines = [
        "WARNING: Deck has not sent anything",
        "no HTTP, clipboard, or export ran",
        "",
        f"schema: {sanitized.get('schema')}  · why: identifies the local allowlist",
        f"generated_at: {sanitized.get('generated_at')}  · why: patrol time, not identity",
        f"healthy: {summary.get('healthy')}  · why: attestation outcome",
        f"reboot_required: {summary.get('reboot_required')}  · why: reboot boundary",
        f"pending: {summary.get('pending')}  · why: queue counts, not package names",
        f"cve_findings: {summary.get('cve_findings')}  · why: count only",
        READ_ONLY_LINE,
    ]
    return "\n".join(lines) + "\n"


def load_patrol_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.is_file():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "finished_at": item.get("finished_at"),
                "healthy": item.get("healthy"),
                "reboot_required": item.get("reboot_required"),
                "pending": item.get("pending") or {},
                "cve_findings": item.get("cve_findings"),
            }
        )
        if len(compact) >= HISTORY_CAP:
            break
    return compact


def append_patrol_history(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not summary:
        return load_patrol_history()
    history = load_patrol_history()
    entry = {
        "finished_at": summary.get("finished_at"),
        "healthy": summary.get("healthy"),
        "reboot_required": summary.get("reboot_required"),
        "pending": summary.get("pending") or {},
        "cve_findings": summary.get("cve_findings"),
    }
    history.insert(0, entry)
    history = history[:HISTORY_CAP]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False) + "\n", encoding="utf-8")
    return history








