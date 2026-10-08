"""The Deck's two schedules: a daily read-only patrol and the weekly unattended update."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .engine import STATE_ROOT, _write_json, attest, load_json, run_capture, snapshot
from .cve import scan_cves

PATROL_UNIT = "arch-update-patrol"
PATROL_TIMER = f"{PATROL_UNIT}.timer"
PATROL_SCHEDULE = "*-*-* 09:30:00"
PATROL_JITTER = "15min"
PATROL_SERVICE = """[Unit]
Description=Arch Update Deck: daily read-only patrol

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:%h/bin:/usr/local/sbin:/usr/local/bin:/usr/bin
ExecStart={launcher} patrol
"""
PATROL_TIMER_UNIT = """[Unit]
Description=Arch Update Deck: daily read-only patrol

[Timer]
OnCalendar={schedule}
Persistent=true
RandomizedDelaySec={jitter}

[Install]
WantedBy=timers.target
"""
AUTO_UNIT = "arch-update-auto"
AUTO_EVERY_DAYS = 3
AUTO_SCHEDULE = "3min after login, then once a day while the session lasts; a visit only if the last one is 3 days old"
AUTO_SERVICE = """[Unit]
Description=Arch Update Deck: unattended update, offered at login every few days

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:%h/bin:/usr/local/sbin:/usr/local/bin:/usr/bin
# Not due: systemd skips the run without marking the unit failed.
ExecCondition={launcher} auto --due
ExecStart={launcher} auto --scheduled
SuccessExitStatus=10 80
# On stop only the run itself is signalled; it lets the package manager finish.
KillMode=mixed
TimeoutStopSec=150min
"""
AUTO_TIMER = """[Unit]
Description=Arch Update Deck: unattended update, offered at login every few days

[Timer]
OnStartupSec=3min
OnUnitInactiveSec=1d

[Install]
WantedBy=timers.target
"""


def last_visit() -> datetime | None:
    """When the owner last answered Update Day's question or an update ran: the newer of the two records."""
    stamps = []
    for name in ("last-auto.json", "ask-result.json"):
        try:
            stamps.append(datetime.fromtimestamp((STATE_ROOT / name).stat().st_mtime).astimezone())
        except OSError:
            continue
    return max(stamps, default=None)


def is_due(now: datetime | None = None, days: int = AUTO_EVERY_DAYS) -> bool:
    """True when no visit is on record or the last one is `days` old; what the service's ExecCondition asks."""
    last = last_visit()
    return last is None or (now or datetime.now().astimezone()) - last >= timedelta(days=days)


def _unit_properties(unit: str) -> dict[str, str] | None:
    result = run_capture(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,NextElapseUSecRealtime,LastTriggerUSec",
        ],
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return {
        key: value
        for line in result.output.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _last_patrol() -> dict[str, Any] | None:
    path = STATE_ROOT / "last-patrol.json"
    if not path.is_file():
        return None
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def last_patrol_report() -> dict[str, Any] | None:
    """Return the local patrol source for an explicitly requested export."""
    return _last_patrol()


def _patrol_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    verification = report.get("verification") or {}
    pending = (report.get("snapshot") or {}).get("updates") or {}
    cve = report.get("cve") or {}
    cve_available = cve.get("available")
    cve_findings = len(cve.get("findings") or []) if cve_available else None
    return {
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "kind": report.get("kind"),
        "healthy": verification.get("healthy"),
        "reboot_required": verification.get("reboot_required"),
        "pending": {"repo": pending.get("repo"), "aur": pending.get("aur")},
        "cve_available": cve_available,
        "cve_findings": cve_findings,
        "snapshot_error": report.get("snapshot_error"),
        "cve_error": report.get("cve_error"),
    }


def scheduler_status() -> dict[str, Any]:
    """Describe the patrol timer from unit properties, not a human table; auto_status() covers the weekly one."""
    manager = run_capture(["systemctl", "--user", "is-system-running"], timeout=10)
    available = manager.returncode == 0 or manager.output.strip() in {"running", "degraded"}
    unit = _unit_properties(PATROL_TIMER) if available else None
    installed = bool(unit and unit.get("LoadState") == "loaded")
    return {
        "available": available,
        "manager": manager.output.strip() or f"exit {manager.returncode}",
        "patrol": {
            "unit": PATROL_TIMER,
            "installed": installed,
            "active": bool(unit and unit.get("ActiveState") == "active"),
            "next": unit.get("NextElapseUSecRealtime") if unit else None,
            "last": unit.get("LastTriggerUSec") if unit else None,
        },
        "cadence": f"daily {PATROL_SCHEDULE[-8:]} + up to {PATROL_JITTER} jitter",
        "last_patrol": _patrol_summary(_last_patrol()),
    }


def _unit_directory(unit_dir: Path | None) -> Path:
    return unit_dir or Path.home() / ".config/systemd/user"


def _launcher() -> str:
    return shutil.which("arch-update") or str(Path.home() / ".local/bin/arch-update")


def _install_unit_pair(unit: str, service: str, timer: str, unit_dir: Path | None) -> Any:
    """Write a service and its timer as unit files and enable the timer. Safe to run again."""
    directory = _unit_directory(unit_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{unit}.service").write_text(service, encoding="utf-8")
    (directory / f"{unit}.timer").write_text(timer, encoding="utf-8")
    # A timer made by an older release with `systemd-run` shadows the files and vanishes on reboot: drop it.
    run_capture(["systemctl", "--user", "stop", f"{unit}.timer"], timeout=15)
    run_capture(["systemctl", "--user", "daemon-reload"], timeout=15)
    return run_capture(["systemctl", "--user", "enable", "--now", f"{unit}.timer"], timeout=15)


def install_daily_patrol_timer(unit_dir: Path | None = None) -> dict[str, Any]:
    """Install the explicit daily read-only patrol as unit files, so it survives a reboot."""
    result = _install_unit_pair(
        PATROL_UNIT,
        PATROL_SERVICE.format(launcher=_launcher()),
        PATROL_TIMER_UNIT.format(schedule=PATROL_SCHEDULE, jitter=PATROL_JITTER),
        unit_dir,
    )
    return {
        "installed": result.returncode == 0,
        "unit": PATROL_TIMER,
        "output": result.output.strip(),
        "promise": "read-only queries only; never installs, never uses sudo, never reboots",
    }


def install_weekly_auto_timer(unit_dir: Path | None = None) -> dict[str, Any]:
    """Install the weekly unattended update as unit files, so it survives a reboot.

    Any exit but IDLE or READY_FOR_REBOOT leaves the service failed: a blocked
    run shows up in `systemctl --user --failed` instead of passing in silence.
    """
    result = _install_unit_pair(
        AUTO_UNIT,
        AUTO_SERVICE.format(launcher=_launcher()),
        AUTO_TIMER,
        unit_dir,
    )
    return {
        "installed": result.returncode == 0,
        "unit": f"{AUTO_UNIT}.timer",
        "schedule": AUTO_SCHEDULE,
        "output": result.output.strip(),
        "promise": "updates every installed store; never reboots, never merges .pacnew",
    }


def remove_timer(unit: str, unit_dir: Path | None = None) -> dict[str, Any]:
    """Stop and delete one of the two timers and its service. Removing what is not there succeeds."""
    if unit not in {PATROL_UNIT, AUTO_UNIT}:
        raise ValueError(f"not one of this tool's timers: {unit}")
    run_capture(["systemctl", "--user", "disable", "--now", f"{unit}.timer"], timeout=15)
    directory = _unit_directory(unit_dir)
    for suffix in ("timer", "service"):
        (directory / f"{unit}.{suffix}").unlink(missing_ok=True)
    reload = run_capture(["systemctl", "--user", "daemon-reload"], timeout=15)
    return {"removed": reload.returncode == 0, "unit": f"{unit}.timer", "output": reload.output.strip()}


def auto_status() -> dict[str, Any]:
    """The weekly timer and how its last run ended."""
    unit = _unit_properties(f"{AUTO_UNIT}.timer")
    try:
        last = load_json(STATE_ROOT / "last-auto.json")
    except (OSError, ValueError):  # no run yet, or a file cut short by a power loss
        last = {}
    return {
        "unit": f"{AUTO_UNIT}.timer",
        "installed": bool(unit and unit.get("LoadState") == "loaded"),
        "active": bool(unit and unit.get("ActiveState") == "active"),
        "next": unit.get("NextElapseUSecRealtime") if unit else None,
        "last_run": {key: last.get(key) for key in ("finished_at", "state", "updated", "trouble", "held")} if last else None,
    }


def run_patrol() -> dict[str, Any]:
    """Write one evidence snapshot for the schedule without touching packages."""
    started = datetime.now().astimezone().isoformat()
    report: dict[str, Any] = {
        "started_at": started,
        "kind": "read-only-patrol",
        "promise": "read-only queries only; never installs, never uses sudo, never reboots",
    }
    try:
        report["snapshot"] = snapshot(include_updates=True, include_slow=True)
        report["verification"] = attest(after=report["snapshot"])
    except (OSError, RuntimeError, ValueError) as exc:
        report["snapshot_error"] = str(exc)
    try:
        report["cve"] = scan_cves()
    except (OSError, RuntimeError, ValueError) as exc:
        report["cve_error"] = str(exc)
    report["finished_at"] = datetime.now().astimezone().isoformat()
    _write_json(STATE_ROOT / "last-patrol.json", report)
    from .surface import append_patrol_history

    append_patrol_history(_patrol_summary(report))
    return report
