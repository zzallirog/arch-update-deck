"""How far a dotfiles repository is behind its upstream, and which of upstream's lines your own edits hold back.

Everything is git's own: `git merge-tree --write-tree` merges in the object
store, never in your folder, and with merge.conflictStyle=zdiff3 every place
where you and upstream changed the same lines comes back as git marks it:

    <<<<<<< yours
    your lines
    ||||||| base
    the lines you both started from
    =======
    upstream's lines
    >>>>>>> upstream

The update keeps your side there (-X ours), so each such place is upstream
held back: listed here, with the lines, until you take or drop them. A file
upstream changed that then failed to parse was given back to you as it was
(syntax.py); it is listed too, with the parser's words.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DRIFT = "drift"
SHOWN_LINES = 400
SHOWN_KEYS = 60
ZDIFF3 = ("-c", "merge.conflictStyle=zdiff3")
# A setting, in the shapes config files take: `key = v`, `key: v`, `export KEY=v`, `set -g key v`, `property int key: v`.
SETTING = (
    re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?(?P<key>[A-Za-z_][\w.\-]*)\s*(?:=|:(?!:))\s*\S"),
    re.compile(r"^\s*set\s+(?:-\w+\s+)*(?P<key>[A-Za-z_]\w*)\s+\S"),
    re.compile(r"^\s*(?:readonly\s+)?property\s+[\w<>.]+\s+(?P<key>\w+)\s*:"),
)
COMMENT = re.compile(r"^\s*(#|//|--|;)")
LISTLIKE = 3  # a key set this many times in one diff is a list (bind, exec-once, windowrule), not one setting


@dataclass
class Drift:
    target: str = ""
    behind: int = 0  # upstream commits not in yours
    upstream_lines: int = 0  # lines upstream changed since you were last level
    held: dict[str, list[str]] = field(default_factory=dict)  # path -> the conflict hunks, git's markers
    structural: list[str] = field(default_factory=list)  # git's words for what has no lines to show (a deletion, a rename)
    unparsed: dict[str, str] = field(default_factory=dict)  # path -> parser's words, upstream's version given back
    shadowed: list[str] = field(default_factory=list)  # a key you set that upstream now also sets somewhere else
    adopted: list[str] = field(default_factory=list)  # a line of yours that upstream now has itself: yours can go

    @property
    def hunks(self) -> int:
        return sum(len(hunks) for hunks in self.held.values())

    @property
    def held_lines(self) -> int:
        """Upstream's lines inside the held places: what you do not have."""
        return sum(len(_theirs(hunk)) for hunks in self.held.values() for hunk in hunks)

    def summary(self, name: str) -> str:
        parts = [f"{self.behind} commits, {self.upstream_lines} lines behind {self.target}"]
        if self.hunks:
            parts.append(f"{self.hunks} upstream changes ({self.held_lines} lines) in {len(self.held)} files held back by your edits")
        if self.unparsed:
            parts.append(f"{len(self.unparsed)} files kept as yours: upstream's did not parse")
        if self.shadowed:
            parts.append(f"{len(self.shadowed)} of your settings upstream now also sets elsewhere")
        if self.adopted:
            parts.append(f"{len(self.adopted)} of your lines are upstream's now")
        return f"{name}: " + "; ".join(parts)


def measure(read: Callable[..., str], head: str, target: str) -> Drift:
    """What merging `target` into `head` would bring and what your side would hold back. Writes nothing to the folder."""
    drift = Drift(target=target)
    drift.behind = int(read("rev-list", "--count", f"{head}..{target}") or 0)
    stat = read("diff", "--shortstat", f"{head}...{target}")
    drift.upstream_lines = sum(int(number) for number in re.findall(r"(\d+) (?:insertion|deletion)", stat))
    out = read(*ZDIFF3, "merge-tree", "--write-tree", "--name-only", head, target).splitlines()
    if not out:
        return drift
    tree, rest = out[0], out[1:]
    names = rest[: rest.index("")] if "" in rest else rest
    messages = rest[len(names) + 1:]
    for path in dict.fromkeys(names):
        hunks = _hunks(read("show", f"{tree}:{path}"))
        if hunks:
            drift.held[path] = hunks
    drift.structural = [line.strip() for line in messages if "CONFLICT" in line and "CONFLICT (content)" not in line]
    base = read("merge-base", head, target)
    if base:
        drift.shadowed, drift.adopted = _settings(read("diff", "-U0", "--no-color", base, head), read("diff", "-U0", "--no-color", base, target))
    return drift


def _added(diff: str) -> list[tuple[str, int, str]]:
    """Every + line of a -U0 diff, as (path, line number on the new side, text). Comments are no settings."""
    found, path, number = [], "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            hunk = re.search(r"\+(\d+)", line)
            number = int(hunk[1]) if hunk else 0
        elif line.startswith("+") and path:
            if line[1:].strip() and not COMMENT.match(line[1:]):
                found.append((path, number, line[1:]))
            number += 1
    return found


def _key(text: str) -> str:
    for shape in SETTING:
        found = shape.match(text)
        if found:
            return found["key"]
    return ""


def _settings(yours: str, theirs: str) -> tuple[list[str], list[str]]:
    """Watch every + on both sides. A key you set that upstream sets somewhere else too is shadowed: git merges
    that without a word, and whichever is read last wins. A line of yours that upstream now has itself is adopted.
    """
    mine, upstream = _added(yours), _added(theirs)
    upstream_lines = {text.strip() for _, _, text in upstream}
    adopted = [f"{path}:{number}: {text.strip()}" for path, number, text in mine if text.strip() in upstream_lines]

    def keyed(lines: list[tuple[str, int, str]]) -> dict[str, list[tuple[str, int, str]]]:
        by_key: dict[str, list[tuple[str, int, str]]] = {}
        for path, number, text in lines:
            key = _key(text)
            if key:
                by_key.setdefault(key, []).append((path, number, text.strip()))
        return {key: places for key, places in by_key.items() if len(places) < LISTLIKE}

    their_keys = keyed(upstream)
    shadowed = []
    for key, places in keyed(mine).items():
        for path, number, text in places:
            if text in upstream_lines:
                continue  # adopted, not shadowed
            for their_path, their_number, their_text in their_keys.get(key, []):
                if (their_path, their_text) != (path, text):
                    where = "same file" if their_path == path else their_path
                    shadowed.append(f"{key}: yours `{text}` ({path}:{number}); upstream now `{their_text}` ({where}:{their_number})")
    same_file_first = sorted(shadowed, key=lambda line: "same file" not in line)
    return same_file_first, adopted


def _hunks(text: str) -> list[str]:
    found, current = [], None
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            current = [line]
        elif current is not None:
            current.append(line)
            if line.startswith(">>>>>>> "):
                found.append("\n".join(current))
                current = None
    return found


def _theirs(hunk: str) -> list[str]:
    lines = hunk.splitlines()
    try:
        start = lines.index("=======") + 1
    except ValueError:
        return []
    return lines[start:-1]


def render(name: str, drift: Drift) -> str:
    """The report a person reads (or gives to whoever helps): where, and git's own picture of each place."""
    text = [f"# {name}", "", drift.summary(name), ""]
    shown = 0
    for path, hunks in drift.held.items():
        text += [f"## {path}: {len(hunks)} held back", "", "```"]
        for hunk in hunks:
            lines = hunk.splitlines()
            if shown + len(lines) > SHOWN_LINES:
                text.append(f"... (more in `git -C <repo> diff HEAD...{drift.target} -- {path}`)")
                break
            text += [*lines, ""]
            shown += len(lines)
        text += ["```", ""]
    if drift.structural:
        text += ["## Without lines to show", "", *[f"- {line}" for line in drift.structural], ""]
    if drift.shadowed:
        text += ["## Your settings upstream now also sets elsewhere", "",
                 "git merges these without a word; whichever line is read last wins. Yours are kept; check the order.", ""]
        text += [f"- {line}" for line in drift.shadowed[:SHOWN_KEYS]]
        text += [f"- ... {len(drift.shadowed) - SHOWN_KEYS} more"] if len(drift.shadowed) > SHOWN_KEYS else []
        text.append("")
    if drift.adopted:
        text += ["## Your lines upstream has now", "", "Upstream made the same change: your own copy of it can go.", ""]
        text += [f"- {line}" for line in drift.adopted[:SHOWN_KEYS]]
        text.append("")
    if drift.unparsed:
        text += ["## Kept as yours: upstream's version did not parse", ""]
        text += [f"- {path}: {why}" for path, why in drift.unparsed.items()]
        text.append("")
    text += [
        "To take one of upstream's changes, edit the file in the repository (the lines under ======= are upstream's) and commit;",
        "the next update sees it as level. To look at all of it the way git does:",
        f"`git -c merge.conflictStyle=zdiff3 merge-tree --write-tree HEAD {drift.target}`.",
        "",
    ]
    return "\n".join(text)


def save(state_root: Path, name: str, drift: Drift) -> Path:
    folder = state_root / DRIFT
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (re.sub(r"[^A-Za-z0-9._-]+", "_", name) + ".md")
    path.write_text(render(name, drift), encoding="utf-8")
    return path
