"""The census: failures that already happened, and what was done about them.

It is not a measurement of this machine. It is the memory a failure is checked
against: has this been seen before, is there a repair that worked, what is the
one command for a human. A failure nobody has seen becomes a new case, so the
next time it is known. Shipped cases live in data/failure-modes.json; cases
first met on this machine live in its state directory, in the same shape, so
one can be moved into the other.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import VAULT_PATH, _write_json, load_json

LOCAL_FILE = "census.json"
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\d+")  # what changes between two meetings with the same failure


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

    def matches(self, output: str) -> bool:
        return bool(self.classify_output and self.match and re.search(self.match, output))


def _case(entry: dict[str, Any]) -> Case:
    return Case(**{key: entry[key] for key in Case.__dataclass_fields__ if key in entry})


def load(state_root: Path) -> list[Case]:
    """Shipped cases first, then the ones this machine added. Position is identity: see token()."""
    cases = [_case(entry) for entry in load_json(VAULT_PATH)["modes"]]
    local = state_root / LOCAL_FILE
    if local.is_file():
        cases += [_case(entry) for entry in load_json(local)]
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

    A case learned on this machine (`arch-update learn`) wins: it was written
    for exactly this failure, after a fix that worked. Otherwise, when several
    cases match, one without a repair wins: a failure is only retried when
    every cause found in it is known to be repairable.
    """
    found = [case for case in cases if case.matches(output) and not (case.store and store is not None and case.store != store)]
    learned = [case for case in found if case.id.startswith("LOCAL-LEARNED-")]
    return learned[0] if learned else next((case for case in found if not case.repair), found[0] if found else None)


def signature(output: str) -> str:
    """The line that names a failure: the last one that says error, else the last one."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    return (errors or lines or ["no output"])[-1][:200]


def record(cases: list[Case], state_root: Path, output: str, where: str, log: Path) -> Case:
    """Add a failure nobody has seen as a new local case, and return it.

    Its pattern is the failure's signature with numbers loosened, so the same
    failure with another version or path is recognised next time.
    """
    line = signature(output)
    # A failure with no output has no line to match on; its pattern is "the output is blank".
    pattern = _VOLATILE.sub(lambda _: r"(?:0x[0-9a-fA-F]+|\d+)", re.escape(line)) if output.strip() else r"\A\s*\Z"
    digest = hashlib.sha256(pattern.encode()).hexdigest()[:8]
    known = next((case for case in cases if case.id == f"LOCAL-{digest}"), None)
    if known is not None:
        met_again(cases, state_root, known)
        return known
    case = Case(
        id=f"LOCAL-{digest}",
        match=pattern,
        fix="arch-update brief",  # the brief points into the log, and says how to save a fix as a case
        meaning=f"first seen {datetime.now().astimezone().date().isoformat()} in {where}: {line}",
        seen=1,
    )
    cases.append(case)
    save_local(cases, state_root)
    return case


def met_again(cases: list[Case], state_root: Path, case: Case) -> None:
    """Count another meeting with a case this machine recorded itself."""
    if case.id.startswith("LOCAL-"):
        case.seen += 1
        save_local(cases, state_root)


def save_local(cases: list[Case], state_root: Path) -> None:
    _write_json(state_root / LOCAL_FILE, [asdict(case) for case in cases if case.id.startswith("LOCAL-")])
