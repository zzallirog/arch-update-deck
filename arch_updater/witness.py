"""Learning by witnessing: a fix somebody ran by hand, seen in pacman.log or in an assistant's transcript.

`arch-update learn` needs a person to type a command, and nobody does. The fixes exist anyway: a failed update
is followed by a package command typed at a terminal, and the next update passes. This module reads that.

- `settle` (the actor of the first source, called at the end of every run): for each open brief whose store
  passed in this run, the package commands pacman.log shows between the failure and this run, which name a
  package the failure names, become a witnessed fix of that failure.
- `from_sessions` (the actor of the second source, called by the weekly scan of transcripts): the proven package
  action that follows a failed one in the same assistant session, the same way.

A witnessed fix is generalized: a package name that stands in the failure line and in the command becomes a
capture group, so the case matches the next pair of packages and its fix names them. It is shown (brief,
notification, `census`, the share offer) and never run: it was a person's decision about one machine, and removing
packages unattended can break the OS. `arch-update learn --accept ID` turns it into a replay after checking it
on the failure it was seen for.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import census, textsafe

PACMAN_LOG = Path("/var/log/pacman.log")
TRUSTED_DIRS = {"/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin"}
MAX_TARGETS = 200  # a command with more is a system change, not the fix for one failure; and a log of them must not stall a run
MAX_COMMANDS = 200
MAX_AGE_DAYS = 60  # a brief older than this is not looked into: too much happened since
GROUP_BARE = r"[A-Za-z0-9@_+][A-Za-z0-9@._+-]{0,63}"  # bounded: a package name is short, and a pattern must stay linear
GROUP_NAMED = r"[A-Za-z0-9@_+][A-Za-z0-9@._+-]{0,63}?"  # followed by -version-release
VERSION = r"\d[\w.+:~]{0,31}-\d{1,6}(?:\.\d{1,3})?"
RUNNING = re.compile(r"^\[(?P<at>[^\]]+)\] \[PACMAN\] Running '(?P<cmd>.*)'\s*$")
TELLING = re.compile(r"(?i)\b(error|fail(ed|ure)?|conflict|denied|not found|cannot|could not|unable|missing|refus|abort|warning)\b")
OPTIONS_WITH_VALUE = {
    "--config", "--root", "-r", "--dbpath", "-b", "--cachedir", "--logfile", "--gpgdir", "--sysroot", "--color", "--ask",
    "--assume-installed", "--overwrite", "--hookdir", "--dbonly-ignore",
}
NAMING_OPTIONS = {"--ignore", "--ignoregroup"}  # their value names a package, but it is not a target
DROPPED = {"--noconfirm", "--config"}  # the first is the person's yes at a prompt, not part of the fix; the second is yay's
SHOW = "arch-update brief"  # what a click on a notice may open for a witnessed fix: the brief that shows it
SKIPPED_BRIEFS = {census.TOOL_REFUSED}  # the tool stopped itself: there is nothing for a person to have fixed


@dataclass
class Hand:
    """One package command somebody typed, as pacman.log shows it."""

    at: datetime
    argv: list[str]
    lines: list[str] = field(default_factory=list)  # the log lines of its transaction, for the evidence
    completed: bool = False


@dataclass
class Pacman:
    """A pacman command line, read: what it does and which packages it names."""

    ops: str  # R, S and/or U
    targets: list[str]  # packages it installs or removes, `repo/` taken off; for -U the name read from the file
    named: list[str]  # packages it only mentions (--ignore)
    argv: list[str]


def when(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z",):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def read_pacman(argv: list[str]) -> Pacman | None:
    """What a pacman command line changes, or None when it changes nothing worth a fix (a query, a refresh, -D, -F)."""
    if not argv or Path(argv[0]).name != "pacman" or ("/" in argv[0] and str(Path(argv[0]).parent) not in TRUSTED_DIRS):
        return None  # the real pacman only: a program of the same name in some other folder is not what was meant
    ops, targets, named, skip, rest = "", [], [], False, False
    flags = ""
    for index, arg in enumerate(argv[1:], 1):
        if skip:
            skip = False
            continue
        if rest or not arg.startswith("-"):
            targets.append(arg)
        elif arg == "--":
            rest = True
        elif arg.startswith("--"):
            name, _, value = arg.partition("=")
            if name in NAMING_OPTIONS:
                if value:
                    named += value.split(",")
                elif index + 1 < len(argv):
                    named += argv[index + 1].split(",")
                    skip = True
            elif name in OPTIONS_WITH_VALUE and not value:
                skip = True
            ops += {"--remove": "R", "--sync": "S", "--upgrade": "U"}.get(name, "")
        else:
            flags += arg[1:]
            if arg in OPTIONS_WITH_VALUE:
                skip = True
    ops += "".join(letter for letter in "RSU" if letter in flags)
    if not ops or any(letter in flags for letter in "DQFT") or len(set(ops)) != 1:
        return None
    names = []
    for target in targets:
        if "U" in ops:
            found = re.fullmatch(r"(?:.*/)?(.+?)-[^-]+-[^-]+-[^-]+\.pkg\.tar(?:\.\w+)?", target)
            if found:
                names.append(found.group(1))
        else:
            names.append(target.split("/", 1)[-1])
    names = [name for name in names if census.NAME.fullmatch(name)]
    if len(names) > MAX_TARGETS:
        return None
    if "S" in ops and not names:
        return None  # -Sy, -Syu: a refresh or a full upgrade, which is the update itself and not a fix for it
    return Pacman(ops, names, [name for name in named if census.NAME.fullmatch(name)], argv)


def hand_commands(text: str, since: datetime, until: datetime) -> list[Hand]:
    """The package commands in pacman.log after `since` and up to `until` that changed packages and finished."""
    found: list[Hand] = []
    current: Hand | None = None
    for line in text.splitlines():
        running = RUNNING.match(line)
        if running:
            stamp = when(running["at"])
            try:
                argv = shlex.split(running["cmd"])
            except ValueError:
                argv = []
            current = Hand(stamp, argv) if stamp is not None and since < stamp <= until and read_pacman(argv) else None
            if current is not None:
                found.append(current)
                current.lines.append(line)
        elif current is not None and line.startswith("["):
            if "[ALPM] transaction completed" in line:
                current.completed = True
            if len(current.lines) < 8 and re.search(r"\[ALPM\] (transaction|removed|installed|upgraded|downgraded)", line):
                current.lines.append(line)
    return [hand for hand in found if hand.completed][-MAX_COMMANDS:]


# ── generalizing: a package name in the failure and in the command becomes a group ────────────────────────────────


@dataclass
class Generalized:
    match: str
    groups: dict[str, str]  # package name -> group name
    line: str  # the failure clause it is read from


def _mentions(core: str, name: str) -> bool:
    return core == name or bool(re.fullmatch(re.escape(name) + r"-\d.*", core))


def _clauses(line: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.?!\]])\s+|\s*::\s*", line) if part.strip()]


def generalize(output: str, names: list[str]) -> Generalized | None:
    """A pattern for the failure in `output` where each package of `names` it speaks of is a capture group, or None.

    The failure's clause around the names is kept (`<pkg> and (?P<pkg>...)-version are in conflict`), so the
    case matches the next pair of packages. None when no name is in the output, or what is left is too little to
    recognise a failure by.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    shared = [name for name in dict.fromkeys(names) if any(_mentions(tok.core, name) for line in lines for tok in census.tokens(line))]
    if not shared:
        return None
    mentioning = [line for line in lines if any(_mentions(tok.core, name) for name in shared for tok in census.tokens(line))]
    line = next((item for item in mentioning if TELLING.search(item)), mentioning[0])

    def present(clause: str) -> list[str]:
        return [name for name in shared if any(_mentions(tok.core, name) for tok in census.tokens(clause))]

    clause = max(_clauses(line) or [line], key=lambda item: len(present(item)))
    if len(clause.split()) < 3:
        clause = line
    here = present(clause)
    groups = {name: ("pkg" if index == 0 else f"pkg{index + 1}") for index, name in enumerate(here)}
    defined: set[str] = set()
    out, literal = "", 0
    toks = census.tokens(clause)
    if toks and (toks[0].kind or any(_mentions(toks[0].core, name) for name in here)):
        out = census.START  # the pattern opens with a hole or a group: tried at the start of a word only
    toks[-1].core = toks[-1].core.rstrip(".") or toks[-1].core  # the full stop that ends a sentence is not part of the failure
    for tok in toks:
        out += (r"[ \t]{1,8}" if out and out != census.START else "") + re.escape(tok.lead)
        owner = next((name for name in here if _mentions(tok.core, name)), None)
        if owner is not None:
            group = groups[owner]
            versioned = tok.core != owner
            if group in defined:
                out += f"(?P={group})"
            else:
                out += f"(?P<{group}>{GROUP_NAMED if versioned else GROUP_BARE})"
                defined.add(group)
            out += "-" + VERSION if versioned else ""
        elif tok.kind:
            out += census._HOLE[tok.kind]
        else:
            literal += 1
            out += census._VOLATILE.sub(lambda _: census._NUMBER, re.escape(tok.core))
        out += re.escape(tok.trail)
    if literal < 2:
        return None
    if not any(all(hit.groupdict().get(group) == name for name, group in groups.items()) for hit in census.finditer(out, output)):
        return None  # no line of the output gives these very names back
    return Generalized(out, groups, clause)


def fix_of(argv: list[str], groups: dict[str, str] | None = None) -> str:
    """The command as a person would type it: `sudo`, no `--noconfirm`, no yay `--config`. With groups, `{pkg}` for names."""
    kept, skip = [], False
    for arg in ["pacman", *argv[1:]]:  # the program is the word pacman, whatever path the log or the transcript gave
        if skip:
            skip = False
        elif arg == "--config":
            skip = True
        elif arg not in DROPPED and not arg.startswith("--config="):
            kept.append(arg)
    parts = []
    for arg in kept:
        bare = arg.split("/", 1)[-1]
        parts.append("{" + groups[bare] + "}" if groups and bare in groups and not arg.startswith("-") else shlex.quote(arg))
    return "sudo " + " ".join(parts)


def unattended(fix: str) -> str:
    """The same fix with the person's yes built in: what a replay needs, since nobody is there to answer.

    A fix is one command or several joined by `&&` (what the person typed, in order). Every pacman command in it gets
    `--noconfirm`; the joints stay what they were, and every word is quoted again as it was.
    """
    lexer = shlex.shlex(fix, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for word in lexer:
        if word == "&&":
            commands.append([])
        elif word and set(word) <= set("();<>|&"):
            return fix  # anything but a plain chain is left exactly as it is
        else:
            commands[-1].append(word)
    return " && ".join(" ".join(shlex.quote(word) for word in _with_yes(words)) for words in commands if words)


def _with_yes(words: list[str]) -> list[str]:
    if "--noconfirm" in words or "pacman" not in words:
        return words
    at = words.index("pacman") + 1
    while at < len(words) and words[at].startswith("-") and not words[at].startswith("--"):
        at += 1
    return [*words[:at], "--noconfirm", *words[at:]]


def instantiate(case: census.Case, output: str) -> tuple[str, str] | None:
    """A witnessed case made exact for one failure: (a pattern that names its packages, the fix with them put in)."""
    found = census.search(case.match, output) if case.match else None
    if found is None:
        return None
    pattern = case.match
    for group, value in found.groupdict().items():
        if not value or not census.NAME.fullmatch(value):
            return None
        pattern = pattern.replace(f"(?P<{group}>{GROUP_BARE})", re.escape(value)).replace(f"(?P<{group}>{GROUP_NAMED})", re.escape(value))
        pattern = pattern.replace(f"(?P={group})", re.escape(value))
    return pattern, case.fix_for(output)


# ── building the case ─────────────────────────────────────────────────────────────────────────────────────────────


def pin(pattern: str, groups: dict[str, str]) -> str:
    """The pattern with each capture group replaced by the package name it captured (`groups`: name -> group)."""
    for name, group in groups.items():
        pattern = pattern.replace(f"(?P<{group}>{GROUP_BARE})", re.escape(name)).replace(f"(?P<{group}>{GROUP_NAMED})", re.escape(name))
        pattern = pattern.replace(f"(?P={group})", re.escape(name))
    return pattern


def until_undone(runs: list[tuple[Pacman, list[str]]]) -> list[tuple[Pacman, list[str]]]:
    """The commands up to the first one that undoes an earlier one: a removal is not followed by a reinstall of what it
    removed, nor an install by the removal of what it installed. What comes after that is somebody changing their mind
    (or the next week), not the fix, so it is left out together with the command that reverses."""
    gone: set[str] = set()
    added: set[str] = set()
    kept: list[tuple[Pacman, list[str]]] = []
    for command, lines in runs:
        names = set(command.targets)
        if ("R" in command.ops and names & added) or (command.ops in ("S", "U") and names & gone):
            break
        (gone if "R" in command.ops else added).update(names)
        kept.append((command, lines))
    return kept


def build(store: str, failure: str, output: str, runs: list[tuple[Pacman, list[str]]], source: str, day: str) -> census.Case | None:
    """The witnessed case for a failure and the commands that followed it, or None when none of them is about it."""
    related = until_undone([(command, lines) for command, lines in runs if generalize(output, command.targets + command.named)])
    if not related:
        return None
    first = generalize(output, related[0][0].targets + related[0][0].named)
    assert first is not None
    fixes = [fix_of(command.argv) for command, _ in related]
    generic = [fix_of(command.argv, first.groups) for command, _ in related]
    match = first.match
    if len(related) > 1:
        # Several commands name several packages: the one pattern cannot carry them all, so the case is pinned to these
        # packages (it recognises the same failure again) and its fix is not a template.
        match, generic = pin(first.match, first.groups), fixes
    safe = lambda text: textsafe.clean(textsafe.secrets(text))  # noqa: E731 - a transcript is not trusted: no escape sequence reaches a terminal
    fix, template = safe(" && ".join(fixes)), safe(" && ".join(generic))
    ident = census.WITNESSED + hashlib.sha256(f"{store}\0{match}\0{template}".encode()).hexdigest()[:8]
    evidence = "\n".join(safe(line) for _, lines in related for line in lines)
    return census.Case(
        id=ident,
        match=match,
        fix=fix,
        generic=template if template != fix else "",
        meaning=f"{store}: after {failure} somebody ran `{fix}` and the next update passed (seen {day} in {source}); shown, never run for you",
        store=store,
        evidence=evidence,
        source=source,
    )


def settle(
    state_root: Path, cases: list[census.Case], done: set[str], started: datetime, log: Path | None = None, now: datetime | None = None
) -> list[census.Case]:
    """The end of a run: each open brief whose store passed now learns what was typed between its failure and this run.

    Returns the witnessed cases that are new. Reads pacman.log; runs nothing.
    """
    from . import briefs

    log = log or PACMAN_LOG
    text: str | None = None
    fresh: list[census.Case] = []
    now = now or datetime.now().astimezone()
    for meta_path in sorted(briefs.root(state_root).glob("*/meta.json")):
        folder = meta_path.parent
        try:
            meta = briefs.load_meta(folder)
            output = (folder / "failure.log").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        since = when(str(meta.get("at", "")))
        if meta.get("learned") or meta.get("witnessed") or since is None or meta.get("store") not in done or meta.get("store") not in ("repo", "aur"):
            continue
        if meta.get("case") in SKIPPED_BRIEFS or (now - since).days > MAX_AGE_DAYS:
            continue
        known = next((case for case in cases if case.id == meta.get("case")), None)
        if known is not None and not known.classify_output:
            continue  # a check's failure (a battery, a lock): there is no command output to match a fix to
        if text is None:
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return fresh
        runs = [(read_pacman(hand.argv), hand.lines) for hand in hand_commands(text, since, started)]
        case = build(meta["store"], str(meta.get("case")), output, [(command, lines) for command, lines in runs if command], "pacman.log", since.date().isoformat())
        if case is None:
            continue
        fresh += census.add_witnessed(cases, state_root, [case])
        meta["witnessed"] = case.id
        try:
            (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        except OSError:
            pass
    return fresh


# ── the second source: what an assistant did, from the weekly scan of transcripts ────────────────────────────────

_CUT = re.compile(r"^(\||>|[0-9]?>|&>)")


def commands_in(command: str) -> list[Pacman]:
    """The pacman commands a shell line holds, `sudo` and anything after a pipe or a redirect taken off."""
    from .transcripts import _split_shell, _tokens

    found = []
    for segment in _split_shell(command):
        words = _tokens(segment)
        for index, word in enumerate(words):
            if _CUT.match(word):
                words = words[:index]
                break
        read = read_pacman(words)
        if read is not None:
            found.append(read)
    return found


def from_sessions(report: dict[str, Any], cases: list[census.Case], state_root: Path) -> list[census.Case]:
    """Witnessed cases from a scan of transcripts: a failed package action, then the proven one that answers it.

    A session's actions are in order. The failed one has failure lines (`signals`) and changed nothing; the next
    action that did change packages, and names a package those lines name, is the fix. Returns the new cases.
    """
    fresh: list[census.Case] = []
    for session in report.get("sessions") or []:
        actions = session.get("actions") or []
        for index, failed in enumerate(actions):
            signals = [str(line) for line in failed.get("signals") or []]
            if failed.get("changed") or not signals:
                continue
            fixing = next((later for later in actions[index + 1 :] if later.get("changed")), None)
            if fixing is None:
                continue
            store = "aur" if str(failed.get("kind", "")).startswith("aur") else "repo"
            commands = commands_in(str(fixing.get("command", "")))
            mode = next(iter(failed.get("failure_modes") or []), "") or "an update"
            case = build(
                store, mode, "\n".join(signals), [(command, [f"{fixing.get('timestamp')} {textsafe.secrets(' '.join(command.argv))}"]) for command in commands],
                f"a {session.get('source')} session", str(fixing.get("timestamp") or "")[:10],
            )
            if case is not None:
                fresh += census.add_witnessed(cases, state_root, [case])
    return fresh
