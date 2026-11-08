#!/usr/bin/env python3
"""Intake of the census: issues with the label `census` become a pull request that adds cases to failure-modes.json.

    tools/intake/intake.py                      # dry run: read the open issues with `gh`, print the diff
    tools/intake/intake.py --issues FILE.json   # the same on a saved `gh issue list --json number,title,body` (a test, an offline look)
    tools/intake/intake.py --pr                 # make the branch, commit and open the pull request (needs `gh` and git rights)

Nothing here runs a fix. A fix in an issue is text from a stranger: it is checked for syntax (`sh -n`), refused if it
looks destructive, written to the catalogue as a command for a human with `"automatic": false` and no `repair`, and a
maintainer reads the pull request before it ships. Only the dry run is the default.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from arch_updater import census, textsafe  # noqa: E402

CATALOGUE = ROOT / "arch_updater" / "data" / "failure-modes.json"
SCHEMA = "arch-update-census/v1"
LABEL = "census"
BRIEF_FIX = census.BRIEF_FIX
MAX_FIX = 400
# Things a shipped fix must never be, whoever wrote it: it is a line a person will paste into a root shell.
DESTRUCTIVE = re.compile(
    r"(?i)(\brm\s+(-\w*\s+)*(-\w*[rf]\w*)\b|\bmkfs\b|\bdd\s+if=|\bshred\b|\bwipefs\b|>\s*/dev/(sd|nvme|mmc)|\bchmod\s+-R\s+\d+\s+/\s|\bchown\s+-R\b.*\s/\s*$"
    r"|\bcurl\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b|\bwget\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b|:\(\)\s*\{|\bpacman\s+-\w*S\w*\s+.*--overwrite\s+['\"]?\*"
    r"|\bsudoers\b|\bvisudo\b|\bpasswd\b|\bfdisk\b|\bparted\b|\bcryptsetup\b)"
)
# A pattern that finds a failure must not find these: lines of an update that went well.
HARMLESS = ("", "ok", "done", ":: Synchronizing package databases...", "there is nothing to do", "resolving dependencies...", "error", "warning", "installing foo (1-1)")
Run = Callable[[list[str]], subprocess.CompletedProcess]


def cases_of(body: str) -> tuple[list[dict[str, Any]], str]:
    """The cases in an issue body (the JSON block of the form) and the tool version, or ([], reason)."""
    block = re.search(r"```json\s*(.*?)```", body or "", re.S)
    if block is None:
        return [], "no ```json block"
    try:
        data = json.loads(block.group(1))
    except ValueError as exc:
        return [], f"the JSON does not parse: {exc}"
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("cases"), list):
        return [], f"not a {SCHEMA} document"
    version = re.search(r"###\s*arch-update version\s*\n+\s*(\S+)", body or "")
    return [item for item in data["cases"] if isinstance(item, dict)], version.group(1) if version else ""


def refusal(case: dict[str, Any], known: list[census.Case]) -> str:
    """Why a shared case does not go in the catalogue, or "" when it may."""
    fix, match = str(case.get("fix") or ""), str(case.get("match") or "")
    if not fix or fix == BRIEF_FIX:
        return "a failure with no fix yet"
    if not match:
        return "no pattern"
    try:
        pattern = re.compile(match)
    except re.error as exc:
        return f"the pattern does not compile ({exc})"
    if len(match) < 8 or any(pattern.search(text) for text in HARMLESS):
        return "the pattern is too broad: it finds lines of a good update"
    if "\n" in fix or len(fix) > MAX_FIX or textsafe.clean(fix) != fix:
        return "the fix is not one plain line"
    if DESTRUCTIVE.search(fix):
        return "the fix looks destructive; a maintainer writes such a case by hand"
    if subprocess.run(["sh", "-n", "-c", fix], capture_output=True).returncode != 0:
        return "the fix is not valid sh"
    if any(item.match == match or item.fix == fix and item.match and re.search(item.match, case.get("meaning", "")) for item in known if item.match):
        return "the catalogue already has it"
    return ""


def entry_of(case: dict[str, Any], number: int, version: str) -> dict[str, Any]:
    """A catalogue entry from a shared case: the pattern and the command for a human, nothing that runs by itself."""
    ident = "CENSUS-" + hashlib.sha256(f"{case['match']}\0{case['fix']}".encode()).hexdigest()[:8].upper()
    meaning = textsafe.clean(str(case.get("meaning") or "A failure met on a machine that shared its census."))[:300]
    entry: dict[str, Any] = {
        "id": ident,
        "match": case["match"],
        "stage": "any",
        "meaning": meaning,
        "response": f"Seen fixed by hand on another machine and shared through census issue #{number} (arch-update {version or 'unknown version'}). The tool does not run it: read the command, then run it yourself if it fits.",
        "automatic": False,
        "fix": case["fix"],
    }
    if case.get("generic") and "{" in str(case["generic"]):
        entry["generic"] = case["generic"]  # the same fix with {pkg} where the pattern captures a package name
    if case.get("store") in ("repo", "aur", "flatpak", "snap"):
        entry["store"] = case["store"]
    entry["origin"] = "community"
    return entry


def merge(text: str, entries: list[dict[str, Any]]) -> str:
    """The catalogue text with the entries added at the end of "modes": the rest of the file is not touched."""
    tail = "\n  ]\n}"
    at = text.rstrip().rindex(tail) if text.rstrip().endswith(tail) else -1
    if at < 0:
        raise ValueError("failure-modes.json does not end the way this tool expects")
    body = ",\n".join("    " + json.dumps(entry, indent=2, ensure_ascii=False).replace("\n", "\n    ") for entry in entries)
    return text.rstrip()[:at] + ",\n" + body + text.rstrip()[at:] + "\n"


def plan(issues: list[dict[str, Any]], catalogue: Path = CATALOGUE) -> dict[str, Any]:
    """What would go in: entries, the new text of the catalogue, and a line for everything that was left out and why."""
    text = catalogue.read_text(encoding="utf-8")
    known = [census.Case(**{key: entry[key] for key in census.Case.__dataclass_fields__ if key in entry}) for entry in json.loads(text)["modes"]]
    entries: list[dict[str, Any]] = []
    left: list[str] = []
    used: list[int] = []
    for issue in issues:
        number = int(issue.get("number") or 0)
        cases, version = cases_of(str(issue.get("body") or ""))
        if not cases:
            left.append(f"#{number}: {version}")
            continue
        for case in cases:
            why = refusal(case, [*known, *(census.Case(id=entry["id"], match=entry["match"], fix=entry["fix"]) for entry in entries)])
            if why:
                left.append(f"#{number} {case.get('id', '?')}: {why}")
                continue
            entries.append(entry_of(case, number, version))
            used.append(number)
    return {"entries": entries, "issues": sorted(set(used)), "left": left, "text": merge(text, entries) if entries else text, "old": text}


def diff(result: dict[str, Any], name: str = "arch_updater/data/failure-modes.json") -> str:
    return "".join(difflib.unified_diff(result["old"].splitlines(True), result["text"].splitlines(True), f"a/{name}", f"b/{name}"))


def gh_issues(repo: str | None, run: Run) -> list[dict[str, Any]]:
    argv = ["gh", "issue", "list", "--label", LABEL, "--state", "open", "--limit", "100", "--json", "number,title,body"]
    done = run(argv + (["--repo", repo] if repo else []))
    if done.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout)


def open_pr(result: dict[str, Any], run: Run, catalogue: Path = CATALOGUE) -> str:
    """Branch, write, commit, push and open the pull request. Returns the pull request's address."""
    stamp = hashlib.sha256("".join(entry["id"] for entry in result["entries"]).encode()).hexdigest()[:8]
    branch = f"census-intake-{stamp}"
    numbers = ", ".join(f"#{number}" for number in result["issues"])
    steps = [["git", "switch", "-c", branch, "origin/main"]]
    for step in steps:
        if run(step).returncode:
            raise RuntimeError(f"{' '.join(step)} failed")
    catalogue.write_text(result["text"], encoding="utf-8")
    body = f"Adds {len(result['entries'])} case(s) shared through census issues {numbers}.\n\nEach fix is a command for a human (`automatic: false`); read every one before merging.\n\nLeft out:\n" + "\n".join(f"- {line}" for line in result["left"])
    for step in (
        ["git", "add", str(catalogue)],
        ["git", "commit", "-m", f"Add census cases from issues {numbers}"],
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--title", f"Census intake: {len(result['entries'])} case(s) from {numbers}", "--body", body, "--base", "main", "--head", branch],
    ):
        done = run(step)
        if done.returncode:
            raise RuntimeError(f"{' '.join(step[:3])} failed: {done.stderr.strip()[-300:]}")
    return done.stdout.strip()


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=ROOT)


def main(argv: list[str] | None = None, run: Run = _run, out: Any = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Turn issues labelled `census` into a pull request on failure-modes.json.")
    parser.add_argument("--issues", type=Path, help="read issues from this JSON file instead of asking gh")
    parser.add_argument("--repo", help="owner/name, for gh (default: the repository of the current folder)")
    parser.add_argument("--catalogue", type=Path, default=CATALOGUE)
    parser.add_argument("--pr", action="store_true", help="make the branch, commit, push and open the pull request; without it only the diff is printed")
    args = parser.parse_args(argv)
    issues = json.loads(args.issues.read_text(encoding="utf-8")) if args.issues else gh_issues(args.repo, run)
    result = plan(issues, args.catalogue)
    for line in result["left"]:
        print(f"left out  {line}", file=out)
    if not result["entries"]:
        print("nothing to add", file=out)
        return 0
    out.write(diff(result))
    if not args.pr:
        print(f"dry run: {len(result['entries'])} case(s) from issues {result['issues']}; pass --pr to open the pull request", file=out)
        return 0
    print(open_pr(result, run, args.catalogue), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
