"""The census: failures that already happened, and what was done about them.

It is not a measurement of this machine. It is the memory a failure is checked
against: has this been seen before, is there a repair that worked, what is the
one command for a human. A failure nobody has seen becomes a new case, so the
next time it is known. Shipped cases live in data/failure-modes.json; cases
first met on this machine live in its state directory, in the same shape, so
one can be moved into the other.

Three kinds of case live in the state directory, and they do not weigh the same:

- LOCAL-<hash>: a failure met here and nothing known about it. Its pattern is the
  failure's signature without paths, versions, package file names or hashes.
- LOCAL-WITNESSED-<hash>: a fix a person (or an assistant) ran by hand, which the
  tool saw in pacman.log or in a transcript. It is shown to the person and never
  run for them: removing packages unattended can break the system.
- LOCAL-LEARNED-<hash>: a fix a person saved with `arch-update learn` after it was
  checked against the failure. Only this kind is replayed by the run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import textsafe
from .engine import VAULT_PATH, _write_json, load_json

LOCAL_FILE = "census.json"
LEARNED = "LOCAL-LEARNED-"
WITNESSED = "LOCAL-WITNESSED-"
TOOL_REFUSED = "TOOL-REFUSED"
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\d+")  # what changes between two meetings with the same failure
_NUMBER = r"(?:0x[0-9a-fA-F]+|\d+)"
NAME = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@._+-]*")  # what pacman calls a package: never an option, never a path
SIGNATURE_CHARS = 160  # a signature is cut at a word below this many characters
BRIEF_FIX = "arch-update brief"  # the fix of a failure nobody has an answer for yet


@dataclass
class Case:
    id: str
    match: str = ""
    repair: str | None = None  # the name of an automatic action, if one is known to help
    fix: str = ""  # one command for a human
    meaning: str = ""
    classify_output: bool = True
    seen: int = 0  # how often this machine met it; only counted for local cases
    where: str = ""  # a learned case: the folder its fix runs in ("" = home)
    store: str = ""  # a learned case: the store it was learned on; it answers no other store's failure
    generic: str = ""  # a witnessed case: the fix with {pkg} where the match captures a package name
    evidence: str = ""  # a witnessed case: the lines it was seen in
    source: str = ""  # a witnessed case: "pacman.log" or the transcript surface it came from

    def matches(self, output: str) -> bool:
        return bool(self.classify_output and self.match and re.search(self.match, output))

    def fix_for(self, output: str) -> str:
        """The command for this very failure: a generalized fix has the package names of `output` put in."""
        if not (self.generic and self.match and output):
            return self.fix
        found = re.search(self.match, output)
        if found is None:
            return self.fix
        text = self.generic
        for name, value in found.groupdict().items():
            if value and NAME.fullmatch(value):
                text = text.replace("{" + name + "}", value)
        return text if "{" not in text else self.fix

    @property
    def witnessed(self) -> bool:
        return self.id.startswith(WITNESSED)

    @property
    def learned(self) -> bool:
        return self.id.startswith(LEARNED)


def _case(entry: dict[str, Any]) -> Case:
    return Case(**{key: entry[key] for key in Case.__dataclass_fields__ if key in entry})


def load(state_root: Path, persist: bool = True) -> list[Case]:
    """Shipped cases first, then the ones this machine added. Position is identity: see token().

    The local file is cleaned on the way in: a failure recorded with a pattern from before the signatures lost their
    paths and versions is recomputed, and cases that now say the same thing are one case (seen counts add up).
    """
    cases = [_case(entry) for entry in load_json(VAULT_PATH)["modes"]]
    local = state_root / LOCAL_FILE
    if local.is_file():
        try:
            entries = load_json(local)
        except (OSError, ValueError):
            entries = []
        mine = [_case(entry) for entry in entries if isinstance(entry, dict) and "id" in entry]
        for case in mine:
            if case.witnessed:
                case.repair = None  # whatever a file says, a witnessed fix is never replayed
        merged = merge(mine)
        cases += merged
        if persist and [asdict(case) for case in merged] != [asdict(case) for case in mine]:
            try:
                save_local(cases, state_root)
            except OSError:
                pass  # read-only state: the cleaned list is still what this run uses
    return cases


def find(cases: list[Case], case_id: str) -> Case:
    return next(case for case in cases if case.id == case_id)


def token(cases: list[Case], case: Case) -> int:
    """A failure as control.tis carries it: minus the case's position in the census."""
    return -(cases.index(case) + 1)


def at(cases: list[Case], value: int) -> Case | None:
    index = -value - 1
    return cases[index] if 0 <= index < len(cases) else None


def recognise(cases: list[Case], output: str, store: str | None = None) -> Case | None:
    """The case this output belongs to, or None if it has never been seen.

    Every kind of knowledge is asked, and the order in which they answer is fixed:

    1. a case learned on this machine (`arch-update learn`): it was written for exactly this failure, after a fix that worked;
    2. a case witnessed here: somebody fixed this by hand once, and the tool saw how;
    3. the tool's own refusal: it stopped itself on purpose, which is not a build failure whatever else the output says;
    4. the shipped catalogue. When several cases match, one without a repair wins: a failure is only retried when
       every cause found in it is known to be repairable. Among those, a community case with a fix for the packages
       named in the failure (`generic`) comes first.
    """
    found = [case for case in cases if case.matches(output) and not (case.store and store is not None and case.store != store)]
    for kind in (lambda case: case.learned, lambda case: case.witnessed, lambda case: case.id == TOOL_REFUSED):
        chosen = next((case for case in found if kind(case)), None)
        if chosen is not None:
            return chosen
    plain = [case for case in found if not case.repair]
    plain.sort(key=lambda case: not case.generic)  # a shipped case that names the packages it was seen for says more than a general one
    return plain[0] if plain else (found[0] if found else None)


def signature(output: str) -> str:
    """The line that names a failure: the last one that says error, else the last one."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    return (errors or lines or ["no output"])[-1][:200]


# ── the signature of a line: what stays when this machine's own names are taken out ──────────────────────────────

_PLACEHOLDER = {"<path>": "path", "<url>": "url", "<pkg>": "pkg", "<hash>": "hash"}
_HOLE = {"path": r"\S+(?:\s+\S+)*?", "url": r"\S+", "pkg": r"\S+", "hash": r"[0-9a-fA-F]{7,64}"}
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
_PKGFILE = re.compile(r".*\.pkg\.tar(?:\.\w+)?")
_PKGVER = re.compile(r"(?=\S*[A-Za-z])[A-Za-z0-9@._+][A-Za-z0-9@._+-]*-\d[\w.+:~]*-\d+(?:\.\d+)?")  # name-version-release
_HASH = re.compile(r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}")
_WORD = re.compile(r"([\[\('\"`]*)(.*?)([\]\)'\",;:?!]*)")


@dataclass
class Tok:
    sep: str  # the white space before the word
    lead: str  # brackets and quotes that open it
    core: str
    trail: str
    kind: str = ""  # "path", "url", "pkg" (name-version-release), "hash" or "" for an ordinary word

    def text(self) -> str:
        return f"{self.lead}{'<' + self.kind + '>' if self.kind else self.core}{self.trail}"


def kind_of(core: str) -> str:
    """What a word is, if it is something that belongs to one machine or one version and not to the failure."""
    if core in _PLACEHOLDER:
        return _PLACEHOLDER[core]
    if _URL.fullmatch(core):
        return "url"
    if _PKGFILE.fullmatch(core) or (core.count("/") and (core.startswith(("/", "~", "./", "../")) or core.count("/") >= 2)):
        return "path"
    if _PKGVER.fullmatch(core):
        return "pkg"
    if _HASH.fullmatch(core):
        return "hash"
    return ""


def tokens(line: str) -> list[Tok]:
    """The line as words, with every path, URL, package file, name-version-release and hash marked. Runs of one kind are one."""
    found: list[Tok] = []
    for match in re.finditer(r"(\s*)(\S+)", line.strip()):
        sep, part = match.groups()
        lead, core, trail = _WORD.fullmatch(part).groups()  # type: ignore[union-attr]
        tok = Tok(sep, lead, core, trail, kind_of(core))
        before = found[-1] if found else None
        if before is not None and tok.kind in ("path", "pkg") and before.kind == tok.kind and not before.trail and not tok.lead:
            before.trail = tok.trail
            continue
        found.append(tok)
    return found


def display(toks: list[Tok]) -> str:
    return "".join(tok.sep + tok.text() for tok in toks)


def normalize(line: str, *, cut_at_path: bool = False) -> str:
    """The line as the census keeps it and shows it: this machine's paths and versions are named `<path>`, `<pkg>`...

    `cut_at_path` is for a line an older version of the tool cut at 200 characters in the middle of a list of paths:
    what follows the first path is not the failure's, so the signature ends before it.
    """
    toks = tokens(line)
    if cut_at_path:
        first = next((index for index, tok in enumerate(toks) if tok.kind in ("path", "url")), None)
        if first is not None and display(toks[:first]).strip():
            return display(toks[:first]).strip()
    shown, size = [], 0
    for tok in toks:
        size += len(tok.sep) + len(tok.text())
        if size > SIGNATURE_CHARS and shown:
            break
        shown.append(tok)
    return display(shown)


def pattern_of(shown: str) -> str:
    """The regular expression for a normalized line: numbers loosened, each `<path>`... a hole that fits any."""
    parts = re.split(r"(<path>|<url>|<pkg>|<hash>)", shown)
    out = ""
    for index, part in enumerate(parts):
        out += _HOLE[_PLACEHOLDER[part]] if index % 2 else _VOLATILE.sub(lambda _: _NUMBER, re.escape(part))
    return out.replace(re.escape(textsafe.HIDDEN), r"[^/?#@&\s]+")  # the hidden part still matches whatever stood there


def pattern_for(output: str) -> tuple[str, str]:
    """(pattern, shown line) for a failure's output: what the census would record, and how it names it."""
    line = textsafe.secrets(signature_full(output))  # a case is kept, shown and shared: no credential of a URL in it
    shown = normalize(line)
    # A failure with no output has no line to match on; its pattern is "the output is blank".
    return (pattern_of(shown) if output.strip() else r"\A\s*\Z"), shown


def signature_full(output: str) -> str:
    """`signature`, but whole: normalizing comes before cutting, so a long list of paths is one `<path>`."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    return (errors or lines or ["no output"])[-1]


def digest_of(pattern: str) -> str:
    return "LOCAL-" + hashlib.sha256(pattern.encode()).hexdigest()[:8]


def record(cases: list[Case], state_root: Path, output: str, where: str, log: Path) -> Case:
    """Add a failure nobody has seen as a new local case, and return it.

    Its pattern is the failure's signature without paths, versions, package file names or hashes and with numbers
    loosened, so the same failure with another package or version is recognised, and counted, next time.
    """
    pattern, line = pattern_for(output)
    known = next((case for case in cases if case.id == digest_of(pattern)), None)
    if known is not None:
        met_again(cases, state_root, known)
        return known
    case = Case(
        id=digest_of(pattern),
        match=pattern,
        fix=BRIEF_FIX,  # the brief points into the log, and says how to save a fix as a case
        meaning=f"first seen {datetime.now().astimezone().date().isoformat()} in {where}: {line}",
        seen=1,
    )
    cases.append(case)
    save_local(cases, state_root)
    return case


_SEEN = re.compile(r"first seen (\d{4}-\d{2}-\d{2}) in ([^:]*): (.*)", re.S)


def _again(case: Case) -> Case:
    """A plain local case with the pattern the present rules would give its line; the case itself when it cannot be told."""
    if not case.id.startswith("LOCAL-") or case.learned or case.witnessed or case.fix != BRIEF_FIX or case.match == r"\A\s*\Z":
        return case
    found = _SEEN.fullmatch(case.meaning)
    if found is None:
        return case
    date, where, line = found.groups()
    # A line of 200 characters or more was cut by the old rule, maybe in the middle of a path: its tail says nothing.
    shown = normalize(line, cut_at_path=len(line) >= 199)
    pattern = pattern_of(shown)
    if pattern == case.match:
        return case
    return Case(**{**asdict(case), "id": digest_of(pattern), "match": pattern, "meaning": f"first seen {date} in {where}: {shown}"})


def merge(local: list[Case]) -> list[Case]:
    """The local cases with old junk recomputed; the ones that now say the same thing are one, `seen` added up."""
    out: list[Case] = []
    for case in map(_again, local):
        twin = next((item for item in out if item.id == case.id), None)
        if twin is None:
            out.append(case)
        else:
            twin.seen += case.seen
    return out


def met_again(cases: list[Case], state_root: Path, case: Case) -> None:
    """Count another meeting with a case this machine recorded itself."""
    if case.id.startswith("LOCAL-"):
        case.seen += 1
        save_local(cases, state_root)


def save_local(cases: list[Case], state_root: Path) -> None:
    _write_json(state_root / LOCAL_FILE, [asdict(case) for case in cases if case.id.startswith("LOCAL-")])


def add_witnessed(cases: list[Case], state_root: Path, new: list[Case]) -> list[Case]:
    """Keep witnessed cases not yet in the census; return the ones that were new. Their repair is None, always."""
    fresh = []
    for case in new:
        if any(item.id == case.id for item in cases):
            continue
        case.repair = None
        cases.append(case)
        fresh.append(case)
    if fresh:
        save_local(cases, state_root)
    return fresh


# ── what the census has collected, in numbers ─────────────────────────────────────────────────────────────────────


def stats(state_root: Path, cases: list[Case] | None = None, sent: set[str] | None = None) -> dict[str, int]:
    """Six numbers: failures met in runs, how many were a case already known, how many a run fixed by itself, and what
    the census holds: fixes witnessed, fixes learned, cases handed upstream. Read from the runs' reports; writes nothing."""
    cases = cases if cases is not None else load(state_root, persist=False)  # counting writes nothing
    if sent is None:
        try:
            sent = set(json.loads((state_root / "census-sent.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            sent = set()
    failures = recognised = fixed = 0
    for path in sorted((state_root / "runs").glob("*/report.json")):
        try:
            report = load_json(path)
        except (OSError, ValueError):
            continue
        if report.get("dry_run"):
            continue
        for item in report.get("trouble") or []:
            failures += 1
            case = str(item.get("case") or "")
            recognised += bool(case) and (not case.startswith("LOCAL-") or case.startswith((LEARNED, WITNESSED)))
        fixed += len(report.get("self_fixed") or [])
    return {
        "failures": failures,
        "recognised": recognised,
        "fixed_by_itself": fixed,
        "witnessed": sum(case.witnessed for case in cases),
        "learned": sum(case.learned for case in cases),
        "shared": sum(case.id in sent for case in cases if case.id.startswith("LOCAL-")),
    }
