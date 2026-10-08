"""Home: is this machine on one of its owner's own Wi-Fi access points?

Home is known by the access point's BSSID, its hardware address, not by the
network's name: anyone can name a network like yours. The BSSID is read, never
set: `nmcli` if NetworkManager answers, otherwise `iw`.

Three answers. Home: a BSSID in `home_bssid`. Away: connected, to another one;
that holds the run back for as long as it lasts. Unknown: not on Wi-Fi (a cable,
a phone over USB), no tool that answers, or a `home_bssid` with no address in
it. Unknown counts as not home, but not for ever: a machine that can never tell
would otherwise never update. After MAX_UNKNOWN_DAYS without one reading that
said home or away, unknown lets the run go and says why. Only the timer's run
keeps that clock (track); the look only reads it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import STATE_ROOT
from .shell import Shell

KEY = "home_bssid"
UNKNOWN_SINCE = "home-unknown.json"  # in the state directory: since when nothing could tell home from away
MAX_UNKNOWN_DAYS = 14
_BSSID = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
# IN-USE is a "*", not a word: ACTIVE says yes or no in the session's language.
NMCLI = ["nmcli", "-t", "-f", "IN-USE,BSSID", "device", "wifi", "list", "--rescan", "no"]


@dataclass(frozen=True)
class Place:
    where: str  # home | away | unknown
    detail: str


def homes(config: dict[str, Any] | None) -> list[str] | None:
    """The configured home BSSIDs, lower case; None when `home_bssid` is not set at all."""
    value = (config or {}).get(KEY)
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return sorted({item.strip().lower() for item in items if isinstance(item, str) and _BSSID.fullmatch(item.strip().lower())})


def connected(sh: Shell) -> list[str] | None:
    """The BSSIDs the Wi-Fi is connected to now ([] when none), or None when no tool could say."""
    if sh.has("nmcli"):
        listed = sh.capture(NMCLI, timeout=10)
        if listed.returncode == 0:
            # Terse output escapes the colons inside a value: "*:AA\:BB\:CC\:DD\:EE\:FF".
            return [line[2:].replace("\\", "").lower() for line in listed.output.splitlines() if line.startswith("*:")]
    if sh.has("iw"):
        devices = sh.capture(["iw", "dev"], timeout=10)
        if devices.returncode == 0:
            found = []
            for name in re.findall(r"^\s*Interface\s+(\S+)", devices.output, re.MULTILINE):
                link = sh.capture(["iw", "dev", name, "link"], timeout=10)
                hit = re.match(r"Connected to ([0-9A-Fa-f:]{17})", link.output.strip()) if link.returncode == 0 else None
                if hit:
                    found.append(hit[1].lower())
            return found
    return None


def locate(config: dict[str, Any] | None, sh: Shell) -> Place | None:
    """Where the machine is, by its access point; None when `home_bssid` is not configured. Reads only."""
    wanted = homes(config)
    if wanted is None:
        return None
    if not wanted:
        return Place("unknown", f"{KEY} holds no address of the form aa:bb:cc:dd:ee:ff")
    try:
        seen = connected(sh)
    except Exception as exc:  # noqa: BLE001 - a reader that breaks cannot tell, it does not decide
        return Place("unknown", f"cannot read the Wi-Fi access point ({type(exc).__name__})")
    if seen is None:
        return Place("unknown", "cannot read the Wi-Fi access point: neither nmcli nor iw answered")
    if not seen:
        return Place("unknown", "not connected to Wi-Fi")
    hit = next((bssid for bssid in seen if bssid in wanted), None)
    if hit:
        return Place("home", f"access point {hit}")
    return Place("away", f"access point {', '.join(seen)} is not in {KEY}")


def _since(state_root: Path) -> float | None:
    try:
        return float(json.loads((state_root / UNKNOWN_SINCE).read_text(encoding="utf-8"))["since"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def overdue(state_root: Path | None = None, now: float | None = None) -> bool:
    """Has nothing told home from away for MAX_UNKNOWN_DAYS? Reads the clock the timer keeps; writes nothing."""
    since = _since(state_root or STATE_ROOT)
    return since is not None and (now or time.time()) - since >= MAX_UNKNOWN_DAYS * 86400


def track(place: Place | None, state_root: Path | None = None, now: float | None = None) -> None:
    """The timer's run keeps the clock: started at the first unknown, cleared by any reading that knew."""
    path = (state_root or STATE_ROOT) / UNKNOWN_SINCE
    try:
        if place is None or place.where != "unknown":
            path.unlink(missing_ok=True)
        elif _since(path.parent) is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"since": now or time.time(), "reason": place.detail}), encoding="utf-8")
    except OSError:
        pass
