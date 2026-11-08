"""Compact, local-first reports that a user may choose to share."""

from __future__ import annotations

import re
import socket
from dataclasses import asdict
from typing import Any

from . import atomic
from .textsafe import clean, secrets

SCHEMA = "arch-update-share/v1"
CENSUS_SCHEMA = "arch-update-census/v1"
UPSTREAM = "https://github.com/zzallirog/arch-update-deck"
URL_BUDGET = 7000  # browsers and GitHub cut longer links; past it the issue says to attach the file instead
SENT = "census-sent.json"  # in the state directory: the local cases already handed upstream
OFFERED = "census-offered.json"  # in the state directory: the offers a person has already been shown
PROVED = "#replayed"  # the second offer of a fix: after it fixed a failure by itself
TEMPLATE = "census.yml"  # the issue form in .github/ISSUE_TEMPLATE: its field ids are the query keys below
BSSID = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")  # a Wi-Fi access point or any MAC


def issue_url(shared: dict) -> str:
    """A new-issue page on GitHub with the census form (`census.yml`) filled in. Opening it sends nothing: the person presses Submit."""
    import json
    from urllib.parse import quote

    from . import __version__

    learned = sum(1 for case in shared["cases"] if case.get("fix") and case["fix"] != "arch-update brief")
    title = f"Census: {learned} learned fixes, {len(shared['cases']) - learned} new failures"
    head = f"{UPSTREAM}/issues/new?title={quote(title)}&template={TEMPLATE}&labels=census&version={quote(__version__)}"
    url = f"{head}&cases={quote(json.dumps(shared, indent=1))}"
    if len(url) > URL_BUDGET:
        note = "Too long for a link: please attach the output of `arch-update census --share --json`."
        url = f"{head}&cases={quote(note)}"
    return url


def _sent(state_root: Any) -> set[str]:
    import json

    try:
        return set(json.loads((state_root / SENT).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def unsent(cases: list[Any], state_root: Any) -> list[Any]:
    """Every local case not yet handed upstream: learned fixes and failures met here alike. The census itself is not touched."""
    sent = _sent(state_root)
    return [case for case in cases if case.id.startswith("LOCAL-") and case.id not in sent]


def mark_sent(cases: list[Any], state_root: Any) -> None:
    """Remember what was handed to the browser for upstream: listed as [x] from then on, and not handed again."""
    import json

    path = state_root / SENT
    atomic.write(path, json.dumps(sorted(_sent(state_root) | {case.id for case in cases})))


def _offers(cases: list[Any], state_root: Any, config: dict | None, proved: Any) -> tuple[set[str], list[tuple[Any, str]]]:
    """What a person was already shown, and each learned fix still owed an offer with the name that offer is kept under."""
    if (config or {}).get("share") is False:
        return set(), []
    import json

    try:
        offered = set(json.loads((state_root / OFFERED).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        offered = set()
    owed = []
    for case in unsent(cases, state_root):
        if not case.id.startswith(("LOCAL-LEARNED-", "LOCAL-WITNESSED-")):
            continue  # a fix is offered; a failure nobody has an answer for is shared with the next `census --share`
        if case.id not in offered:
            owed.append((case, case.id))
        elif case.id in proved and case.id + PROVED not in offered:
            owed.append((case, case.id + PROVED))
    return offered, owed


def pending_offers(cases: list[Any], state_root: Any, config: dict | None = None, proved: Any = ()) -> list[Any]:
    """The learned and witnessed fixes a person has not been asked about yet. Writes nothing: asking is the caller's part.

    A fix is offered when it is learned or witnessed, and once more the first time it fixes a failure by itself (`proved`: the
    cases replayed in a run that ended well). After that, never again; none with "share": false; none already sent.
    """
    return [case for case, _ in _offers(cases, state_root, config, proved)[1]]


def should_offer(cases: list[Any], state_root: Any, config: dict | None = None, proved: Any = ()) -> list[Any]:
    """`pending_offers`, remembered as shown: call it only where a person does see the question.

    Sharing is optional and apart from the update itself: this only decides whether the question is asked, and
    remembers that it was. Nothing is ever sent from here.
    """
    import json

    offered, owed = _offers(cases, state_root, config, proved)
    if owed:
        path = state_root / OFFERED
        atomic.write(path, json.dumps(sorted(offered | {name for _, name in owed})))
    return [case for case, _ in owed]


def mark(case: Any, state_root: Any, sent: set[str] | None = None) -> str:
    """`*` official (shipped with the tool), `[x]` local and handed upstream, `[ ]` local and only here."""
    if not case.id.startswith("LOCAL-"):
        return "*  "
    return "[x]" if case.id in (sent if sent is not None else _sent(state_root)) else "[ ]"


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def share_census(cases: list[Any], home: str, user: str, host: str | None = None) -> dict:
    """What this machine learned, ready to send upstream: every local case, with what is only this machine's taken out.

    Out: the home folder (`~`), the login (`<user>`), the host name (`<host>`), the credentials of URLs, Wi-Fi access
    points and other MAC addresses (`<bssid>`) and every escape sequence. Shipped cases are not repeated. Each local
    case keeps its pattern, what it means, how often it was met, and, for one learned with `arch-update learn` or
    witnessed, the command that fixed it and the lines it was seen in: the part a shared catalogue is made of.
    """
    host = hostname() if host is None else host
    names = [name for name in dict.fromkeys([host, host.split(".")[0]]) if len(name) >= 3 and name.lower() != "localhost"]

    def scrub(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = secrets(clean(value))
        text = text.replace(home, "~") if home else text
        text = BSSID.sub("<bssid>", text)
        for name in names:
            text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", "<host>", text, flags=re.I)
        return re.sub(rf"\b{re.escape(user)}\b", "<user>", text) if user else text

    local = [case for case in cases if case.id.startswith("LOCAL-")]
    keep = ("id", "match", "meaning", "fix", "generic", "repair", "store", "where", "seen", "evidence", "source")
    return {
        "schema": CENSUS_SCHEMA,
        "cases": [{**{key: scrub(value) for key, value in asdict(case).items() if key in keep and value not in ("", None)}, "origin": "local"} for case in local],
    }


def share_line(cases: list[Any], state_root: Any, config: dict | None = None) -> str:
    """One line for Update Day while something this machine collected has not been handed upstream; "" when nothing is owed.

    It stays until the cases are sent (or `"share": false` in the config), so the offer reaches a person who missed the
    notice. Nothing is sent from here.
    """
    if (config or {}).get("share") is False:
        return ""
    owed = unsent(cases, state_root)
    if not owed:
        return ""
    fixes = sum(1 for case in owed if case.id.startswith(("LOCAL-LEARNED-", "LOCAL-WITNESSED-")))
    parts = [f"{fixes} fix{'es' if fixes != 1 else ''}"] if fixes else []
    if len(owed) > fixes:
        parts.append(f"{len(owed) - fixes} new failure{'s' if len(owed) - fixes != 1 else ''}")
    return f"share: {' and '.join(parts)} from this machine not passed on yet. `arch-update census --share` opens a filled-in GitHub issue; you press Submit"


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
