"""Patrol history: the last few read-only patrols, kept as small summaries."""

from __future__ import annotations

import json
from typing import Any

from .engine import STATE_ROOT

HISTORY_PATH = STATE_ROOT / "patrol-history.json"
HISTORY_CAP = 12


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
