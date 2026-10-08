"""A failure nothing here could fix, prepared for whoever fixes it.

The updater does not call an AI. When a run ends with something still broken,
it leaves a brief beside the census: the lines of the log that matter, where to
look, the cases already known, the rules a fix must keep, and the one command
that turns a working fix into a census case. A person reads it with
`arch-update brief`, or pastes it to whatever AI they talk to; either way the
fix is saved with `arch-update learn`, checked against the very failure it was
written for, and the next run replays it on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import census
from .engine import STATE_ROOT

BRIEFS = "briefs"
EXCERPT_LINES = 100
CONTEXT = 2
# Lines worth reading first in a long log.
TELLING = re.compile(r"(?i)\b(error|errors|fail(ed|ure)?|fatal|conflict|denied|not found|no such|cannot|could not|unable|missing|refus|abort|warning)\b")
LEARNED = "LOCAL-LEARNED-"
REF = re.compile(r"(refs/(tags|remotes)/)?[A-Za-z0-9_][A-Za-z0-9_./-]*")  # a tag or a branch; never an option, never a path out
RULES = """- The person's own changes win over upstream wherever both touch the same thing; upstream's changes are kept everywhere else.
- Dotfiles follow upstream's newest release tag, or its branch when the repository is set to track one. The repository's installer, when one is configured, runs after every update, with no terminal.
- A fix is only finished once it is saved as a census case (below): the next run then replays it by itself, with nobody asked."""


def root(state_root: Path | None = None) -> Path:
    """Read when called, not when imported: a test or ARCH_UPDATER_STATE moves it."""
    return (state_root or STATE_ROOT) / BRIEFS


def excerpts(output: str, limit: int = EXCERPT_LINES) -> list[str]:
    """The telling lines with a little context, numbered as in the output, at most `limit` lines; the tail when nothing tells."""
    lines = output.splitlines()
    keep: set[int] = set()
    for number, line in enumerate(lines):
        if TELLING.search(line):
            keep.update(range(max(0, number - CONTEXT), min(len(lines), number + CONTEXT + 1)))
    chosen = sorted(keep)[-limit:] or list(range(max(0, len(lines) - 20), len(lines)))
    shown: list[str] = []
    for previous, number in zip([None, *chosen], chosen):
        if previous is not None and number != previous + 1:
            shown.append("   ...")
        shown.append(f"{number + 1:>5}  {lines[number]}")
    return shown


def suggested_match(output: str) -> str:
    """The pattern the census itself would use for this failure: its signature line, numbers loosened."""
    line = census.signature(output)
    return census._VOLATILE.sub(lambda _: r"\d+", re.escape(line))


def write(
    store: str,
    case: census.Case,
    output: str,
    run_id: str,
    log: Path | None,
    where: str = "",
    look: list[str] | None = None,
    cases: list[census.Case] | None = None,
    state_root: Path | None = None,
    retry: list[str] | None = None,
    facts: dict[str, str] | None = None,
) -> Path:
    """Leave a brief for one failure and return its folder.

    `retry` is shown to the reader only: `learn` builds the command it runs from the store's name, never from this folder.
    `facts` is what `learn` needs to reproduce the failure (for a dotfiles merge: the target and the rollback tag).
    """
    folder = root(state_root) / f"{run_id}-{re.sub(r'[^A-Za-z0-9._-]+', '_', store)}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "failure.log").write_text(output, encoding="utf-8")
    meta = {"store": store, "case": case.id, "run_id": run_id, "where": where, "at": datetime.now().astimezone().isoformat(), "learned": None, **(facts or {})}
    (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    known = [item for item in cases or [] if item.match and _finds(item.match, output)]
    places = [f"- failing output, whole: {folder / 'failure.log'}"]
    if log is not None:
        places.append(f"- the run's log, every command in order: {log}")
        places.append(f"- the run's events: {log.parent / 'tokens.jsonl'}")
    if where:
        places.append(f"- where the fix runs: {where}")
    places += [f"- {line}" for line in look or []]
    command = shlex.join(["arch-update", "learn", "--brief", str(folder), "--match", suggested_match(output), "--how", "<what was wrong and what fixes it>", "--command", "<one sh command that fixes it>"])
    text = "\n".join(
        [
            f"# {store}: {case.id}",
            "",
            f"Run {run_id}. {case.meaning or 'A failure this machine has not seen before.'}",
            "",
            "## Where to look",
            *places,
            "",
            "## The lines that matter",
            "```",
            *excerpts(output),
            "```",
            "",
            "## Known cases this output matches",
            *([f"- {item.id}: {item.meaning} (fix: {item.fix})" for item in known] or ["- none: this is new"]),
            "",
            "## Rules a fix keeps",
            RULES,
            "",
            "## Fix it and save it in one step",
            "`learn` runs the command with `sh -c`" + (f" in {where}" if where else " in the home folder")
            + (f", then redoes the failed step (`{shlex.join(retry)}`)" if retry else "")
            + ", and saves the case only if that passes. The pattern must find this failure in failure.log."
            + " From then on the same failure is fixed by that command with nobody asked.",
            "```",
            command,
            "```",
            "",
        ]
    )
    (folder / "brief.md").write_text(text, encoding="utf-8")
    return folder


def newest(state_root: Path | None = None) -> Path | None:
    """The latest brief nobody has saved a fix for yet."""
    folders = sorted((path for path in root(state_root).glob("*/meta.json")), key=lambda path: path.stat().st_mtime, reverse=True)
    return next((path.parent for path in folders if not json.loads(path.read_text(encoding="utf-8")).get("learned")), None)


def learn(
    folder: Path, match: str, how: str, command: str, state_root: Path | None = None, run: Any = None, followed: set[str] | None = None
) -> census.Case:
    """Fix, check and save in one step: run the command, redo the failed step, keep the case only if that passes.

    Refused before anything runs unless the pattern finds the failure the brief was written for. `run` runs one
    argv and returns (exit status, output); the default runs it for real.
    """
    state_root = state_root or STATE_ROOT
    run = run or _run
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    output = (folder / "failure.log").read_text(encoding="utf-8")
    try:
        found = _finds(match, output)
    except re.error as exc:
        raise ValueError(f"the pattern is not a regular expression: {exc}") from None
    if not found:
        raise ValueError(f"the pattern does not find the failure in {folder / 'failure.log'}")
    if not command.strip():
        raise ValueError("a case needs a command that fixes it")
    from .auto import CONFIG_PATH, retry_for  # auto writes briefs; this is only needed when one is answered
    from .dotfiles import repos
    from .engine import load_json

    dotfiles = meta["store"].startswith("dotfiles:")
    where = meta.get("where", "")
    # The brief folder is writable by the account: it names nothing that runs. Its repository must be one you follow,
    # its refs plain names, and a store's fix runs in the home folder.
    if followed is None:
        followed = {str(repo.path) for repo in repos(load_json(CONFIG_PATH) if CONFIG_PATH.is_file() else None)}
    if dotfiles and str(Path(where).expanduser()) not in followed:
        raise ValueError(f"{where or 'this brief'} is not a repository under \"dotfiles\" in {CONFIG_PATH}; nothing run")
    if not dotfiles and where:
        raise ValueError("a package store's fix runs in the home folder; this brief names another place; nothing run")
    for ref in (meta.get("rollback"), meta.get("target")):
        if ref is not None and not REF.fullmatch(str(ref)):
            raise ValueError(f"{ref!r} is not a plain ref name; nothing run")
    # A merge that failed was undone: the fix is tried on the failure itself, reproduced in a throwaway worktree.
    # A repository fixed by hand meanwhile then proves nothing about the command, so it is not what is checked.
    try:
        reproduced = dotfiles and meta.get("rollback") and meta.get("target") and _reproduce_merge(where, meta["rollback"], meta["target"], match, command)
    except subprocess.SubprocessError as exc:
        raise ValueError(f"git did not answer while reproducing the failure ({type(exc).__name__}), nothing saved") from None
    if not reproduced:  # anything else: run the fix where it belongs, then redo the step
        status, said = run(["env", "-C", str(Path(where or "~").expanduser()), "sh", "-c", command])
        if status != 0:
            raise ValueError(f"the command failed (exit {status}), nothing saved:\n{said.strip()[-1500:]}")
    case = census.Case(
        id=LEARNED + hashlib.sha256(f"{meta['store']}\0{match}".encode()).hexdigest()[:8],
        match=match,
        repair=None if dotfiles else "replay",  # a dotfiles step replays it itself, in its repository
        fix=command.strip(),
        meaning=f"{meta['store']}: {how}".strip(),
        classify_output=not dotfiles,  # a dotfiles case speaks for its step only, never for a pacman failure
        where=where,
        store=meta["store"],
        seen=0,
    )
    cases = census.load(state_root)
    # The unknown case recorded for this very failure is what the fix answers: it goes, the lesson stays.
    unknown = [item for item in cases if item.id.startswith("LOCAL-") and not item.id.startswith(LEARNED) and item.match and _finds(item.match, output)]
    kept = [item for item in cases if item not in unknown and item.id != case.id]
    census.save_local([*kept, case], state_root)
    retry = retry_for(meta["store"], where)
    if retry and not reproduced:
        # The step itself, once more: a dotfiles step replays the case saved just now; a store runs its own update.
        status, said = run(retry)
        if status != 0:
            census.save_local(cases, state_root)  # as it was: a fix that does not hold is not a case
            raise ValueError(f"the failed step still fails with this fix (exit {status}), nothing saved:\n{said.strip()[-1500:]}")
    meta["learned"] = case.id
    (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return case


def _reproduce_merge(where: str, rollback: str, target: str, match: str, command: str) -> bool:
    """Redo the failed merge in a worktree at `rollback`, run the fix there, and say whether it settled the merge.

    False when the failure does not come back there (an ignored file, say: not in a worktree). Raises when it
    does come back and the fix leaves it unsettled. The person's repository is only read.
    """
    from .dotfiles import IDENTITY

    repo = Path(where).expanduser()
    work = Path(tempfile.mkdtemp(prefix="arch-update-learn-"))
    tree = work / "tree"

    def git(*args: str, cwd: Path = repo) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(cwd), *IDENTITY, *args], capture_output=True, text=True, errors="replace", timeout=300, check=False)

    try:
        if git("worktree", "add", "-q", "--detach", str(tree), rollback).returncode:
            return False
        merged = git("merge", "--no-edit", "--no-verify", "-X", "ours", target, cwd=tree)
        if merged.returncode == 0 or not _finds(match, merged.stdout + merged.stderr):
            return False
        try:
            fixed = subprocess.run(["sh", "-c", command], cwd=tree, capture_output=True, text=True, errors="replace", timeout=600, check=False)
        except subprocess.TimeoutExpired:
            raise ValueError("the command ran past 10 minutes on the reproduced failure, nothing saved") from None
        unmerged = git("diff", "--name-only", "--diff-filter=U", cwd=tree).stdout.strip()
        still_open = git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=tree).returncode == 0
        released = git("merge-base", "--is-ancestor", target, "HEAD", cwd=tree).returncode == 0  # an abort settles nothing
        if fixed.returncode or unmerged or still_open or not released:
            said = (fixed.stdout + fixed.stderr).strip()[-1500:]
            raise ValueError(f"on the failure itself (a worktree at {rollback}) the release is still not merged, nothing saved:\n{said or unmerged}")
        return True
    finally:
        for tidy in (lambda: git("worktree", "remove", "--force", str(tree)), lambda: shutil.rmtree(work, ignore_errors=True), lambda: git("worktree", "prune")):
            try:  # one step that fails must not keep the others from running
                tidy()
            except (OSError, subprocess.SubprocessError):
                pass


def _run(argv: list[str]) -> tuple[int, str]:
    """At the person's terminal: sudo may ask for a password here, so `-n` is dropped."""
    if argv[:2] == ["sudo", "-n"]:
        argv = ["sudo", *argv[2:]]
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=3600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout


def _finds(pattern: str, output: str) -> bool:
    return bool(re.search(pattern, output))


def show(folder: Path) -> str:
    return (folder / "brief.md").read_text(encoding="utf-8")


def load_meta(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "meta.json").read_text(encoding="utf-8"))
