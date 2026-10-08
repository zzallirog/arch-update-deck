"""Repositories installed from git (dotfiles such as end-4), kept at their newest release.

One pass per repository named under "dotfiles" in auto.json: fetch, merge the
newest release tag over whatever the person changed, run the repository's
installer, run its check. Conflicts are settled the usual way, in order: a case
this machine learned for the same failure is replayed; the person's side wins
every line both sides touched (git merge -X ours). What is still broken after
that is reported with a brief (briefs.py), and a merge that could not be
finished is put back as it was.

The installer runs after every move, with no terminal: whatever it would ask,
nobody answers. A failed installer or check goes the same way: replay, else a
brief. Nothing here calls an AI; the brief is what a person or their AI reads,
and `arch-update learn` is how the fix they found becomes the next replay.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import census, syntax
from . import drift as drifts
from .engine import STATE_ROOT

if TYPE_CHECKING:
    from .auto import World

# A release tag: 1.2, v0.56.2, 2026.10.01. Release candidates and betas are not releases.
RELEASE = re.compile(r"v?\d+(\.\d+)*")
LOCAL_BRANCH = "arch-update-local"
LEARNED = "LOCAL-LEARNED-"
# Its own name, and no signing: a commit that waits for a pinentry nobody sees would hold the run.
IDENTITY = ("-c", "user.name=arch-update", "-c", "user.email=arch-update@localhost", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false")
GIT_TIMEOUT = 300
FETCHES = 4  # repositories fetched at once
# An installer or check that waits on something that never comes must not hold the week's run.
TIMEOUTS = {"install": 1800, "check": 300, "replay": 600}
PENDING = "arch-update-install-pending"  # in the repository's .git: an install is owed since the last move
# Desktops and widgets change shape between releases: a month between visits (owner 2026-10-08), the stores a week.
EVERY_DAYS = 30
LAST = "dotfiles.json"  # in the state directory: each repository's last visit that left nothing broken


def due(repo: Repo, state_root: Path, config: dict[str, Any] | None, now: float | None = None) -> bool:
    """True when the repository's last good visit is `dotfiles_every_days` old, or never was, or an install is owed."""
    if (repo.path / ".git" / PENDING).exists():
        return True
    try:
        last = float(json.loads((state_root / LAST).read_text(encoding="utf-8"))[str(repo.path)])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    every = float((config or {}).get("dotfiles_every_days", EVERY_DAYS))
    return (now or time.time()) - last >= every * 86400


def visited(repo: Repo, state_root: Path) -> None:
    path = state_root / LAST
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    seen[str(repo.path)] = time.time()
    path.write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class Repo:
    path: Path
    install: tuple[str, ...] = ()
    check: tuple[str, ...] = ()
    track: str = "release"  # release: the newest release tag, or the branch when there is none · branch: upstream's tip

    @property
    def name(self) -> str:
        """The whole path: two clones that share a folder name are still two repositories."""
        home = str(Path.home())
        path = str(self.path)
        return "dotfiles:" + ("~" + path[len(home):] if path == home or path.startswith(home + "/") else path)


@dataclass
class Outcome:
    moved: bool = False  # the repository is now at another commit
    case: str = ""  # the census case of what is still broken, "" when nothing is
    output: str = ""  # the whole failing output, for the brief
    target: str = ""
    rollback: str = ""  # the tag the repository stood at before this run touched it
    look: list[str] = field(default_factory=list)  # where a person should look, for the brief
    held: list[str] = field(default_factory=list)  # what failed without stopping anything (the check)
    replayed: list[str] = field(default_factory=list)  # learned cases whose command fixed a step
    drift: drifts.Drift = field(default_factory=lambda: drifts.Drift())  # how far behind, and what your edits hold back


def _argv(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(shlex.split(value)) if isinstance(value, str) else tuple(str(word) for word in value)


def repos(config: dict[str, Any] | None) -> list[Repo]:
    """The repositories auto.json names: a path, or {"path", "install", "check", "track"}."""
    found = []
    for entry in (config or {}).get("dotfiles") or []:
        entry = {"path": entry} if isinstance(entry, str) else entry
        found.append(Repo(Path(entry["path"]).expanduser(), _argv(entry.get("install")), _argv(entry.get("check")), entry.get("track", "release")))
    return found


def prefetch(world: World, wanted: list[Repo]) -> dict[Path, Any]:
    """Fetch every repository at once. A fetch waits on the network and uses no CPU, so they overlap freely."""
    def fetch(repo: Repo) -> Any:
        return world.sh.capture(["git", "-C", str(repo.path), *IDENTITY, "fetch", "--tags", "--force", "--prune", "origin"], timeout=600)

    ready = [repo for repo in wanted if (repo.path / ".git").exists()]
    if not ready or world.dry_run:
        return {}
    with ThreadPoolExecutor(max_workers=min(FETCHES, len(ready))) as pool:
        return dict(zip((repo.path for repo in ready), pool.map(fetch, ready)))


def measure(world: World, repo: Repo) -> str:
    """Fetch, then say how far behind the repository is and what your edits hold back. Moves nothing but git's remote refs."""
    if not (repo.path / ".git").exists():
        return f"{repo.name}: not a git repository"

    def read(*args: str) -> str:
        return world.sh.capture(["git", "-C", str(repo.path), *IDENTITY, *args], timeout=120).output.strip()

    def ok(*args: str) -> bool:
        return world.sh.capture(["git", "-C", str(repo.path), *IDENTITY, *args], timeout=120).returncode == 0

    fetched = world.sh.capture(["git", "-C", str(repo.path), *IDENTITY, "fetch", "--tags", "--force", "--prune", "origin"], timeout=600)
    target = _target(read, ok, repo.track)
    if not target:
        return f"{repo.name}: no release tag and no upstream branch" + ("" if fetched.returncode == 0 else " (the fetch failed)")
    if ok("merge-base", "--is-ancestor", target, "HEAD"):
        return f"{repo.name}: level with {target}"
    found = drifts.measure(read, "HEAD", target)
    dirty = " Uncommitted edits are not counted." if read("status", "--porcelain") else ""
    return f"{found.summary(repo.name)}.{dirty}\n  {drifts.save(STATE_ROOT, repo.name, found)}"


def update(world: World, repo: Repo, fetched: Any = None) -> Outcome:
    """Bring one repository to its newest release and install it. Never raises for what git or the installer says.

    `fetched` is the fetch prefetch() already ran for it; without one the fetch happens here.
    """
    outcome = Outcome()

    def git(*args: str) -> list[str]:
        return ["git", "-C", str(repo.path), *IDENTITY, *args]

    def read(*args: str) -> str:
        return world.sh.capture(git(*args), timeout=120).output.strip()

    def ok(*args: str) -> bool:
        return world.sh.capture(git(*args), timeout=120).returncode == 0

    if not (repo.path / ".git").exists():
        return _broken(outcome, "DOTFILES-NOT-A-REPO", f"{repo.path} is not a git repository")
    if fetched is None:
        fetched = world.command(git("fetch", "--tags", "--force", "--prune", "origin"), timeout=600)
    else:
        world.note("fetch", repo=str(repo.path), ok=fetched.returncode == 0, output=fetched.output.strip()[-300:])
    if fetched.returncode:
        return _broken(outcome, "DOTFILES-FETCH", fetched.output)
    outcome.target = target = _target(read, ok, repo.track)
    if not target:
        return _broken(outcome, "DOTFILES-FETCH", f"{repo.path}: no release tag and no upstream branch")
    pending = Path(read("rev-parse", "--absolute-git-dir") or repo.path / ".git") / PENDING
    if ok("merge-base", "--is-ancestor", target, "HEAD"):
        # Already there, or ahead of it: nothing moves backwards. An install that failed after the last move is still owed.
        return _install(world, repo, outcome, target, pending) if pending.exists() else outcome
    before = read("rev-parse", "HEAD")
    gitdir = pending.parent
    busy = [name for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply") if (gitdir / name).exists()]
    if busy:  # the person is in the middle of something: committing now would freeze their conflict markers
        outcome.look = [f"repository {repo.path}: a {busy[0]} is in progress; finish or abort it, nothing was touched"]
        return _broken(outcome, "DOTFILES-UNSAVED", f"{repo.path}: {', '.join(busy)} in progress")
    # A file marked assume-unchanged hides its edits from `add -A` and `status`: a reset would lose them.
    hidden = [line[2:] for line in read("ls-files", "-v").splitlines() if line[:1].islower()]
    if hidden:
        outcome.look = [f"repository {repo.path}: files marked assume-unchanged hold edits git does not show; nothing was touched",
                        f"see them: git -C {shlex.quote(str(repo.path))} ls-files -v | grep '^[a-z]'"]
        return _broken(outcome, "DOTFILES-UNSAVED", f"assume-unchanged: {', '.join(hidden[:20])}")
    rollback = f"arch-update/before-{world.run_id}"
    branch = ""
    if not read("symbolic-ref", "-q", "HEAD"):
        # A checked-out tag: the merge needs a branch to land on. A new one, so no earlier branch loses its commits.
        branch = LOCAL_BRANCH if not ok("rev-parse", "-q", "--verify", f"refs/heads/{LOCAL_BRANCH}") else f"{LOCAL_BRANCH}-{world.run_id}"
        world.command(git("checkout", "-q", "-b", branch), GIT_TIMEOUT)
    if read("status", "--porcelain"):
        world.command(git("add", "-A"), GIT_TIMEOUT)
        saved = world.command(git("commit", "-q", "--no-verify", "-m", f"arch-update: the person's changes before {target}"), GIT_TIMEOUT)
        if saved.returncode or read("status", "--porcelain"):
            # Nothing past this point may run: an undo would reset changes that are in no commit.
            outcome.look = [f"repository {repo.path}: your changes are where they were, uncommitted; nothing was merged"]
            return _broken(outcome, "DOTFILES-UNSAVED", saved.output or read("status", "--short"))
    if world.command(git("tag", "-f", rollback, "HEAD"), GIT_TIMEOUT).returncode:
        return _broken(outcome, "DOTFILES-UNSAVED", f"could not tag {rollback}; nothing was merged")
    outcome.rollback = rollback
    # What upstream brings and what your edits will hold back, measured in git's object store before anything moves.
    outcome.drift = drifts.measure(read, "HEAD", target)
    report = drifts.save(STATE_ROOT, repo.name, outcome.drift)

    def overwritten() -> str | None:
        """git overwrites an ignored file without a word when the merge brings a tracked one to its path."""
        ignored = set(read("ls-files", "--others", "--ignored", "--exclude-standard").splitlines())
        clash = sorted(ignored & set(read("ls-tree", "-r", "--name-only", target).splitlines()))
        return f"error: {target} tracks files kept here as ignored, the merge would overwrite them: {', '.join(clash)}" if clash else None

    clash = overwritten()
    if clash is not None and _heal(world, repo, outcome, clash, overwritten) is not None:
        outcome.look = [f"repository {repo.path}: your ignored files that upstream now tracks are untouched; nothing was merged", clash]
        return _broken(outcome, "DOTFILES-CONFLICT", clash)
    merged = world.command(git("merge", "--no-edit", "--no-verify", "-X", "ours", target), GIT_TIMEOUT)
    if merged.returncode and _heal(world, repo, outcome, merged.output, lambda: None if _merged(read, ok, target) else merged.output) is not None:
        unmerged = read("diff", "--name-only", "--diff-filter=U").splitlines()
        world.command(git("merge", "--abort"), GIT_TIMEOUT)
        world.command(git("reset", "-q", "--hard", rollback), GIT_TIMEOUT)
        if branch:  # it was a checked-out tag: back to exactly that
            world.command(git("checkout", "-q", "--detach", rollback), GIT_TIMEOUT)
            world.command(git("branch", "-q", "-D", branch), GIT_TIMEOUT)
        outcome.look = [
            f"repository {repo.path}, put back to tag {rollback} (the person's changes are committed there)",
            f"the files that did not merge: {', '.join(unmerged) or 'see the log'}",
            f"redo the merge by hand: git -C {shlex.quote(str(repo.path))} merge -X ours {target}",
            f"every place you and upstream both changed, as git marks it: {report}",
        ]
        return _broken(outcome, "DOTFILES-CONFLICT", merged.output)
    outcome.moved = read("rev-parse", "HEAD") != before
    outcome.drift.unparsed = _keep_parsing(world, repo, git, read, rollback)
    report = drifts.save(STATE_ROOT, repo.name, outcome.drift)
    if outcome.drift.hunks or outcome.drift.unparsed:
        outcome.held.append(f"{outcome.drift.summary(repo.name)} (arch-update drift, or {report})")
    return _install(world, repo, outcome, target, pending)


def _keep_parsing(world: World, repo: Repo, git: Callable[..., list[str]], read: Callable[..., str], rollback: str) -> dict[str, str]:
    """Every file the merge changed must still parse. One that does not, while yours did, is given back to you as it was.

    Upstream's change to it stays out until it parses; drift.py lists it with the parser's words. A file new from
    upstream has no version of yours to give back and stays; one that already failed to parse before is not upstream's doing.
    """
    given: dict[str, str] = {}
    for rel in read("diff", "--name-only", rollback, "HEAD").splitlines():
        path = repo.path / rel
        if not path.is_file() or path.is_symlink():
            continue
        problem = syntax.check(path)
        if problem is None:
            continue
        before = subprocess.run(git("show", f"{rollback}:{rel}"), capture_output=True, timeout=GIT_TIMEOUT, check=False)
        if before.returncode:
            continue
        with tempfile.TemporaryDirectory(prefix="arch-update-parse-") as scratch:
            old = Path(scratch) / path.name
            old.write_bytes(before.stdout)
            if syntax.check(old, syntax.kind(path)) is not None:
                continue
        world.command(git("checkout", rollback, "--", rel), GIT_TIMEOUT)
        given[rel] = problem
        world.note("unparsed", repo=str(repo.path), path=rel, why=problem)
    if given:
        world.command(git("commit", "-q", "--no-verify", "-m", f"arch-update: kept your version where upstream's did not parse: {', '.join(given)}"), GIT_TIMEOUT)
    return given


def _install(world: World, repo: Repo, outcome: Outcome, target: str, pending: Path) -> Outcome:
    """Run the installer, then the check. Until the installer succeeds, `pending` says it is owed, run after run."""
    if repo.install:
        pending.touch()
        outcome.held += _shape(world, repo)
        failed = _step(world, repo, outcome, repo.install, TIMEOUTS["install"])
        if failed is not None:
            outcome.look = [f"repository {repo.path}, now at {target}; it was at tag arch-update/before-<run> before", f"the installer: {shlex.join(repo.install)}"]
            return _broken(outcome, "DOTFILES-INSTALL", failed)
    pending.unlink(missing_ok=True)
    if repo.check and _step(world, repo, outcome, repo.check, TIMEOUTS["check"]) is not None:
        outcome.held.append(f"{repo.name}: the check still fails after the update, see the log")
    return outcome


def _target(read: Callable[..., str], ok: Callable[..., bool], track: str) -> str:
    if track == "release":
        # By number, the "v" dropped: git's own version sort puts v1.1 above 2026.10.01 and above 1.6.
        tags = [tag for tag in read("tag", "--list").splitlines() if RELEASE.fullmatch(tag)]
        if tags:
            return f"refs/tags/{max(tags, key=lambda tag: tuple(int(part) for part in tag.lstrip('v').split('.')))}"
    branch = read("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if not branch and ok("rev-parse", "-q", "--verify", "refs/remotes/origin/main"):
        branch = "origin/main"
    return branch


def _shape(world: World, repo: Repo) -> list[str]:
    """Every ~/.config/<name> the repository ships as a folder is a folder before its installer runs.

    A file or a dead link left at that name keeps the program from reading the config inside. It is moved
    aside with the run's name, never deleted, and an empty folder takes its place for the installer to fill.
    """
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    listed = world.sh.capture(["git", "-C", str(repo.path), "ls-files"], timeout=120).output.splitlines()
    names = {path.split("/.config/", 1)[1].split("/", 1)[0] for path in ("/" + line for line in listed) if "/.config/" in path and path.split("/.config/", 1)[1].count("/")}
    moved = []
    for name in sorted(names):
        place = config / name
        if place.is_dir() or not (place.exists() or place.is_symlink()):
            continue  # a folder already, or nothing there: the installer makes it
        aside = place.with_name(f"{name}.arch-update-{world.run_id}")
        place.rename(aside)
        place.mkdir()
        world.note("shape", path=str(place), moved_to=str(aside))
        moved.append(f"{repo.name}: {place} was a file or a dead link, not a folder; moved to {aside}")
    return moved


def _merged(read: Callable[..., str], ok: Callable[..., bool], target: str) -> bool:
    """True only when the release is in: no path unmerged, no merge still open, and `target` an ancestor of HEAD.

    The last is what makes `git merge --abort` (or a reset) no fix: they leave nothing open, and nothing merged.
    """
    return (
        not read("diff", "--name-only", "--diff-filter=U")
        and not read("rev-parse", "-q", "--verify", "MERGE_HEAD")
        and ok("merge-base", "--is-ancestor", target, "HEAD")
    )


def _step(world: World, repo: Repo, outcome: Outcome, argv: tuple[str, ...], timeout: int) -> str | None:
    """Run the installer or the check, and replay a learned fix if it fails. Returns the last failing output, or None."""

    def run() -> str | None:
        result = world.command(["env", "-C", str(repo.path), *argv], timeout)
        return None if result.returncode == 0 else result.output

    failed = run()
    return None if failed is None else _heal(world, repo, outcome, failed, run)


def _heal(world: World, repo: Repo, outcome: Outcome, output: str, retry: Callable[[], str | None]) -> str | None:
    """Replay every case learned for this repository whose pattern finds the failure, until one works.

    `retry` runs the step again (or checks it) and returns its failing output, or None. Returns the same.
    """
    if world.dry_run:
        return output
    for case in learned(world.cases, repo):
        if not re.search(case.match, output):
            continue
        world.note("replay", repo=str(repo.path), case=case.id)
        world.command(["env", "-C", str(repo.path), "sh", "-c", case.fix], TIMEOUTS["replay"])
        if retry() is None:
            outcome.replayed.append(case.id)
            census.met_again(world.cases, STATE_ROOT, case)
            return None
    return output


def learned(cases: list[census.Case], repo: Repo) -> list[census.Case]:
    """The cases `arch-update learn` saved for this repository."""
    return [case for case in cases if case.id.startswith(LEARNED) and case.fix and case.where == str(repo.path)]


def _broken(outcome: Outcome, case: str, output: str) -> Outcome:
    outcome.case, outcome.output = case, output
    return outcome
