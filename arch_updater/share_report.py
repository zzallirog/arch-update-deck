"""Compact, local-first reports that a user may choose to share."""

from __future__ import annotations


SCHEMA = "arch-update-share/v1"


def _count(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return None


def sanitize_patrol_report(report: dict) -> dict:
    """Keep only stable patrol evidence; never copy machine-specific payloads."""
    snapshot = report.get("snapshot") or {}
    updates = snapshot.get("updates") or {}
    verification = report.get("verification") or {}
    cve = report.get("cve") or {}
    modes: list[str] = []

    for issue in verification.get("issues") or []:
        mode = issue.get("mode") if isinstance(issue, dict) else None
        if isinstance(mode, str) and mode not in modes:
            modes.append(mode)

    available = cve.get("available")
    return {
        "schema": SCHEMA,
        "generated_at": report.get("finished_at"),
        "summary": {
            "healthy": verification.get("healthy"),
            "reboot_required": verification.get("reboot_required"),
            "pending": {"repo": _count(updates.get("repo")), "aur": _count(updates.get("aur"))},
            "cve_available": available,
            "cve_findings": _count(cve.get("findings")) if available is True else None,
            "issue_modes": modes,
        },
    }
