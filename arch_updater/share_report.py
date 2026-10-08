"""Compact, local-first reports that a user may choose to share."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .textsafe import clean

SCHEMA = "arch-update-share/v1"
CENSUS_SCHEMA = "arch-update-census/v1"
UPSTREAM = "https://github.com/zzallirog/arch-update-deck"
URL_BUDGET = 7000  # browsers and GitHub cut longer links; past it the issue says to attach the file instead
SENT = "census-sent.json"  # in the state directory: the local cases already handed upstream
OFFERED = "census-offered.json"  # in the state directory: the offers a person has already been shown
PROVED = "#replayed"  # the second offer of a fix: after it fixed a failure by itself


def issue_url(shared: dict) -> str:
    """A new-issue page on GitHub with the census filled in. Opening it sends nothing: the person presses Submit."""
    import json
    from urllib.parse import quote

    learned = sum(1 for case in shared["cases"] if case.get("fix") and case["fix"] != "arch-update brief")
    title = f"Census: {learned} learned fixes, {len(shared['cases']) - learned} new failures"
    body = "From `arch-update census --share` (home folder and login taken out).\n\n```json\n" + json.dumps(shared, indent=1) + "\n```\n"
    url = f"{UPSTREAM}/issues/new?title={quote(title)}&body={quote(body)}"
    if len(url) > URL_BUDGET:
        body = "Too long for a link: please attach the output of `arch-update census --share --json`.\n"
        url = f"{UPSTREAM}/issues/new?title={quote(title)}&body={quote(body)}"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(_sent(state_root) | {case.id for case in cases})), encoding="utf-8")


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
        if not case.id.startswith("LOCAL-LEARNED-"):
            continue
        if case.id not in offered:
            owed.append((case, case.id))
        elif case.id in proved and case.id + PROVED not in offered:
            owed.append((case, case.id + PROVED))
    return offered, owed


def pending_offers(cases: list[Any], state_root: Any, config: dict | None = None, proved: Any = ()) -> list[Any]:
    """The learned fixes a person has not been asked about yet. Writes nothing: asking is the caller's part.

    A fix is offered when it is learned, and once more the first time it fixes a failure by itself (`proved`: the
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(offered | {name for _, name in owed})), encoding="utf-8")
    return [case for case, _ in owed]


def mark(case: Any, state_root: Any, sent: set[str] | None = None) -> str:
    """`*` official (shipped with the tool), `[x]` local and handed upstream, `[ ]` local and only here."""
    if not case.id.startswith("LOCAL-"):
        return "*  "
    return "[x]" if case.id in (sent if sent is not None else _sent(state_root)) else "[ ]"


def share_census(cases: list[Any], home: str, user: str) -> dict:
    """What this machine learned, ready to send upstream: every local case, the home folder and the login taken out.

    Shipped cases are not repeated. Each local case keeps its pattern, what it means, how often it was met, and,
    for one learned with `arch-update learn`, the command that fixed it: the part a shared catalogue is made of.
    """
    def scrub(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = clean(value).replace(home, "~") if home else clean(value)
        return re.sub(rf"\b{re.escape(user)}\b", "<user>", text) if user else text

    local = [case for case in cases if case.id.startswith("LOCAL-")]
    keep = ("id", "match", "meaning", "fix", "repair", "store", "where", "seen")
    return {
        "schema": CENSUS_SCHEMA,
        "cases": [{**{key: scrub(value) for key, value in asdict(case).items() if key in keep}, "origin": "local"} for case in local],
    }


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
