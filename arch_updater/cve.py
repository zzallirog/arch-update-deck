"""Read-only CVE inventory for the local Arch package database."""

from __future__ import annotations

import json
import shutil
from typing import Any

from .engine import run_capture


def scan_cves() -> dict[str, Any]:
    """Return arch-audit findings without changing package state."""
    binary = shutil.which("arch-audit")
    if not binary:
        return {"available": False, "findings": [], "error": "arch-audit not installed"}
    result = run_capture([binary, "--json", "--color", "never"], timeout=60)
    if result.returncode not in (0, 1):
        return {"available": True, "findings": [], "error": result.output.strip() or f"exit {result.returncode}"}
    try:
        findings = json.loads(result.output)
    except json.JSONDecodeError:
        return {"available": True, "findings": [], "error": "arch-audit returned invalid JSON"}
    if not isinstance(findings, list):
        return {"available": True, "findings": [], "error": "arch-audit JSON is not an array"}
    return {"available": True, "findings": findings, "count": len(findings)}
