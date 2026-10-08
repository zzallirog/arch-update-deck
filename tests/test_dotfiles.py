"""Dotfiles kept at their newest release, and the loop that learns: failure, brief, learn, replay.

Real git repositories in a temporary folder; the real shell runs every command.
"""

from __future__ import annotations

import json
import time
import shutil
import subprocess
from pathlib import Path

import pytest

from arch_updater import briefs, census, dotfiles, repairs, syntax
from arch_updater.shell import Shell

ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=false"]
KEEP_MINE = "git add -A && git -c user.name=t -c user.email=t@t commit -q --no-edit"


def git(where: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(where), *ID, *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(where: Path, message: str, **files: str | None) -> None:
    for name, text in files.items():
        if text is None:
            git(where, "rm", "-q", name)
        else:
            (where / name).parent.mkdir(parents=True, exist_ok=True)
            (where / name).write_text(text)
            git(where, "add", name)
    git(where, "commit", "-q", "-m", message)


def ancestor(where: Path, commit_ish: str) -> bool:
    return subprocess.run(["git", "-C", str(where), "merge-base", "--is-ancestor", commit_ish, "HEAD"]).returncode == 0


class World:
    """The part of auto.World that dotfiles and the replay repair talk to, with a real shell."""

    def __init__(self, tmp: Path, cases: list[census.Case] | None = None) -> None:
        self.sh, self.config, self.cases, self.run_id, self.dry_run = Shell(), {}, cases or [], "auto-test", False
        self.run_dir = tmp / "run"
        self.run_dir.mkdir(exist_ok=True)
        self.failure = 0
        self.notes: list[tuple[str, dict]] = []

    def note(self, kind: str, **details) -> None:
        self.notes.append((kind, details))

    def command(self, argv, timeout=None):
        return self.sh.logged(argv, self.run_dir / "run.log", timeout)


@pytest.fixture(autouse=True)
def retry(monkeypatch):
    """What `learn` runs to redo a step: nothing, unless a test says so. Never a real update."""
    from arch_updater import auto

    chosen: dict[str, list[str] | None] = {"argv": None}
    monkeypatch.setattr(auto, "retry_for", lambda store, where="", sh=None: chosen["argv"])
    return chosen


@pytest.fixture
def ground(tmp_path, monkeypatch):
    """Upstream with v1.0, v1.1 and a newer commit that is no release; the person's clone at v1.0."""
    monkeypatch.setattr(dotfiles, "STATE_ROOT", tmp_path / "state")
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q")
    commit(up, "one", **{"bar.conf": "height=30\ncolor=red\n", "old.conf": "a\n", "install.sh": 'touch "$1"\n'})
    git(up, "tag", "v1.0")
    commit(up, "two", **{"bar.conf": "height=40\ncolor=blue\n", "new.conf": "n\n"})
    git(up, "tag", "v1.1")
    commit(up, "tip", **{"bar.conf": "height=40\ncolor=green\n"})
    mine = tmp_path / "mine"
    subprocess.run(["git", *ID, "clone", "-q", str(up), str(mine)], check=True)
    git(mine, "checkout", "-q", "v1.0")
    return up, mine


def deleted_upstream(up: Path, mine: Path, name: str, tag: str) -> None:
    """Upstream deletes a file the person changed: modify/delete, which -X ours cannot settle."""
    commit(up, f"drop {name}", **{name: None})
    git(up, "tag", tag)
    (mine / name).write_text("mine\n")


def test_newest_release_not_the_tip_and_the_person_wins_the_line_both_changed(ground, tmp_path) -> None:
    _, mine = ground
    (mine / "bar.conf").write_text("height=30\ncolor=black\n")  # mine: color; upstream: height and color
    marker = tmp_path / "installed"
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine, install=("sh", "install.sh", str(marker))))
    assert not outcome.case and outcome.moved and outcome.target == "refs/tags/v1.1"
    assert ancestor(mine, "v1.1") and not ancestor(mine, "origin/main")
    assert "color=black" in (mine / "bar.conf").read_text()
    assert (mine / "new.conf").is_file() and marker.is_file()


def test_a_repository_already_there_is_left_alone_and_nothing_moves_backwards(ground, tmp_path) -> None:
    _, mine = ground
    git(mine, "checkout", "-q", "origin/main")  # ahead of the newest release
    head = git(mine, "rev-parse", "HEAD")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert not outcome.case and not outcome.moved and git(mine, "rev-parse", "HEAD") == head


def test_an_unsettled_conflict_is_put_back_as_it_was_and_says_where_to_look(ground, tmp_path) -> None:
    up, mine = ground
    deleted_upstream(up, mine, "old.conf", "v1.2")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.case == "DOTFILES-CONFLICT" and "modify/delete" in outcome.output
    assert (mine / "old.conf").read_text() == "mine\n"
    assert not git(mine, "status", "--porcelain")
    assert git(mine, "rev-parse", "HEAD") == git(mine, "rev-parse", "arch-update/before-auto-test")
    assert any("old.conf" in line for line in outcome.look)


def test_failure_brief_learn_replay(ground, tmp_path) -> None:
    """The loop the updater learns by: the second meeting with a failure is fixed with nobody asked."""
    up, mine = ground
    state = tmp_path / "state"
    deleted_upstream(up, mine, "old.conf", "v1.2")
    repo = dotfiles.Repo(mine)
    first = dotfiles.update(World(tmp_path), repo)
    assert first.case == "DOTFILES-CONFLICT"

    facts = {"target": first.target, "rollback": first.rollback}
    folder = briefs.write(repo.name, census.Case(id=first.case), first.output, "auto-test", tmp_path / "run" / "run.log", str(mine), first.look, state_root=state, facts=facts)
    text = briefs.show(folder)
    assert "CONFLICT (modify/delete)" in text and "arch-update learn --brief" in text and str(mine) in text
    assert briefs.newest(state) == folder

    # A command that does not settle the failure is refused: learn tries it on the failure itself, in a worktree.
    with pytest.raises(ValueError, match="not merged"):
        briefs.learn(folder, r"CONFLICT \(modify/delete\)", "garbage", "true", state_root=state, followed={str(mine)})
    # Someone found the fix, and saves it.
    case = briefs.learn(folder, r"CONFLICT \(modify/delete\)", "keep the person's file", KEEP_MINE, state_root=state, followed={str(mine)})
    assert not git(mine, "worktree", "list").count("\n")  # the throwaway worktree is gone
    assert briefs.newest(state) is None
    stored = census.load(state)
    assert case.id in [item.id for item in stored] and case.where == str(mine)

    # The next run: same failure, nobody there. The census replays the fix.
    again = dotfiles.update(World(tmp_path, stored), repo)
    assert not again.case and again.replayed == [case.id] and ancestor(mine, "v1.2")
    assert (mine / "old.conf").read_text() == "mine\n"

    # And the time after, a new release with the same kind of conflict: replayed again.
    deleted_upstream(up, mine, "new.conf", "v1.3")
    third = dotfiles.update(World(tmp_path, census.load(state)), repo)
    assert not third.case and third.replayed == [case.id] and ancestor(mine, "v1.3")


def test_a_fix_whose_pattern_does_not_find_its_own_failure_is_refused(tmp_path) -> None:
    folder = briefs.write("dotfiles:x", census.Case(id="DOTFILES-INSTALL"), "error: theme not found\n", "auto-test", None, str(tmp_path), state_root=tmp_path)
    with pytest.raises(ValueError, match="does not find"):
        briefs.learn(folder, "something else", "how", "true", state_root=tmp_path, followed={str(tmp_path)})
    with pytest.raises(ValueError, match="needs a command"):
        briefs.learn(folder, "theme not found", "how", "  ", state_root=tmp_path, followed={str(tmp_path)})
    assert not (tmp_path / census.LOCAL_FILE).exists()


def test_a_learned_store_case_is_recognised_first_and_replayed_by_the_engine(tmp_path) -> None:
    # PACMAN-REPLACEMENT-CONFLICT (shipped, no repair) matches too: the case learned here must still win.
    output = "error: failed retrieving file 'x.db' from mirror\n:: a and b are in conflict\n"
    unknown = census.Case(id="LOCAL-0000", match="failed retrieving", fix="arch-update brief")
    (tmp_path / census.LOCAL_FILE).write_text(json.dumps([census.asdict(unknown)]))
    folder = briefs.write("repo", unknown, output, "auto-test", None, state_root=tmp_path)
    case = briefs.learn(folder, r"failed retrieving file", "a stale mirror list", f"touch {tmp_path / 'fixed'}", state_root=tmp_path)
    cases = census.load(tmp_path)
    assert "LOCAL-0000" not in [item.id for item in cases]  # the unknown case it answers is gone
    assert case.repair == "replay" and census.recognise(cases, output).id == case.id

    assert (tmp_path / "fixed").is_file()  # learn ran the fix itself before saving it
    (tmp_path / "fixed").unlink()
    world = World(tmp_path, cases)
    world.failure = census.token(cases, case)
    assert repairs.ACTIONS["replay"](world) and (tmp_path / "fixed").is_file()


def test_a_fix_that_does_not_make_the_step_pass_is_not_kept(tmp_path, retry) -> None:
    # The brief's own idea of the retry is never run: only what the code builds from the store's name.
    folder = briefs.write("repo", census.Case(id="LOCAL-1"), "error: x\n", "auto-test", None, state_root=tmp_path, retry=["touch", str(tmp_path / "planted")])
    retry["argv"] = ["false"]
    with pytest.raises(ValueError, match="still fails"):
        briefs.learn(folder, "error: x", "how", "true", state_root=tmp_path)
    assert not [case for case in census.load(tmp_path) if case.id.startswith(briefs.LEARNED)]
    assert briefs.newest(tmp_path) == folder  # still waiting for a fix
    with pytest.raises(ValueError, match="command failed"):
        briefs.learn(folder, "error: x", "how", "exit 3", state_root=tmp_path)
    assert not (tmp_path / "planted").exists()


def test_an_install_that_failed_is_owed_until_it_succeeds(ground, tmp_path) -> None:
    _, mine = ground
    gate = tmp_path / "gate"
    repo = dotfiles.Repo(mine, install=("test", "-e", str(gate)))
    first = dotfiles.update(World(tmp_path), repo)
    assert first.case == "DOTFILES-INSTALL" and ancestor(mine, "v1.1")
    gate.touch()
    second = dotfiles.update(World(tmp_path), repo)  # nothing new upstream, but the install is still owed
    assert not second.case and not second.moved
    assert not (mine / ".git" / dotfiles.PENDING).exists()


def test_a_dotfiles_case_never_classifies_another_store_failure(tmp_path) -> None:
    folder = briefs.write("dotfiles:x", census.Case(id="DOTFILES-INSTALL"), "error: failed\n", "auto-test", None, str(tmp_path), state_root=tmp_path)
    briefs.learn(folder, "error", "how", "true", state_root=tmp_path, followed={str(tmp_path)})
    assert census.recognise(census.load(tmp_path), "error: failed to commit transaction") is None


def test_excerpts_keep_the_telling_lines_with_context_and_their_numbers() -> None:
    log = "\n".join([*(f"ok {n}" for n in range(50)), "fatal: bad object", *(f"ok {n}" for n in range(50))])
    shown = briefs.excerpts(log)
    assert any(line.endswith("fatal: bad object") and line.strip().startswith("51") for line in shown)
    assert len(shown) <= 2 * briefs.CONTEXT + 1


def test_learn_from_the_command_line_saves_the_case_and_refuses_a_wrong_pattern(tmp_path, monkeypatch, capsys) -> None:
    from arch_updater import main as cli

    monkeypatch.setattr(briefs, "STATE_ROOT", tmp_path)
    briefs.write("repo", census.Case(id="LOCAL-1"), "error: theme not found\n", "auto-test", None)
    assert cli.main(["learn", "--match", "nope", "--how", "x", "--command", "true"]) == 1
    assert cli.main(["learn", "--match", "theme not found", "--how", "x", "--command", "true"]) == 0
    assert "saved LOCAL-LEARNED-" in capsys.readouterr().out
    assert [case.fix for case in census.load(tmp_path) if case.id.startswith(briefs.LEARNED)] == ["true"]
    assert cli.main(["brief"]) == 0 and "no brief waiting" in capsys.readouterr().out


def test_an_ignored_file_upstream_starts_to_track_is_never_overwritten(ground, tmp_path) -> None:
    up, mine = ground
    (mine / ".git" / "info" / "exclude").write_text("secret.conf\n")
    (mine / "secret.conf").write_text("token = mine\n")
    commit(up, "track it", **{"secret.conf": "token = upstream\n"})
    git(up, "tag", "v1.2")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.case == "DOTFILES-CONFLICT" and "secret.conf" in outcome.output
    assert (mine / "secret.conf").read_text() == "token = mine\n"


def test_a_commit_that_fails_stops_everything_and_loses_nothing(ground, tmp_path) -> None:
    """A pre-commit hook that refuses everything: the run's own commit skips it (--no-verify), and nothing is lost."""
    up, mine = ground
    deleted_upstream(up, mine, "old.conf", "v1.2")
    hooks = mine / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    git(mine, "config", "core.hooksPath", str(hooks))
    (mine / "bar.conf").write_text("mine, uncommitted\n")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert (mine / "bar.conf").read_text() == "mine, uncommitted\n" and (mine / "old.conf").read_text() == "mine\n"
    assert not outcome.case or outcome.case in ("DOTFILES-CONFLICT", "DOTFILES-UNSAVED")


def test_a_commit_that_really_fails_is_unsaved_and_nothing_is_merged(ground, tmp_path, monkeypatch) -> None:
    up, mine = ground
    commit(up, "next", **{"bar.conf": "height=50\n"})
    git(up, "tag", "v1.2")
    (mine / "bar.conf").write_text("mine, uncommitted\n")
    (mine / ".git" / "index.lock").write_text("")  # the commit cannot happen
    head = git(mine, "rev-parse", "HEAD")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.case == "DOTFILES-UNSAVED"
    assert (mine / "bar.conf").read_text() == "mine, uncommitted\n" and git(mine, "rev-parse", "HEAD") == head


def test_release_order_is_by_number_whatever_the_v(ground, tmp_path) -> None:
    up, mine = ground
    for tag, text in (("v1.6", "a"), ("2026.10.01", "b"), ("1.10", "c")):
        commit(up, tag, **{"order.txt": text})
        git(up, "tag", tag)
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.target == "refs/tags/2026.10.01"


def test_a_tag_checkout_is_given_back_exactly_after_an_undo(ground, tmp_path) -> None:
    up, mine = ground
    git(mine, "branch", dotfiles.LOCAL_BRANCH)  # left from an earlier run, with its own commit
    git(mine, "checkout", "-q", dotfiles.LOCAL_BRANCH)
    commit(mine, "kept", **{"kept.txt": "x"})
    kept = git(mine, "rev-parse", "HEAD")
    git(mine, "checkout", "-q", "v1.0")
    deleted_upstream(up, mine, "old.conf", "v1.2")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.case == "DOTFILES-CONFLICT"
    assert git(mine, "rev-parse", f"refs/heads/{dotfiles.LOCAL_BRANCH}") == kept  # the old branch is untouched
    assert subprocess.run(["git", "-C", str(mine), "symbolic-ref", "-q", "HEAD"]).returncode != 0  # detached again


def test_two_clones_with_one_folder_name_are_two_repositories(tmp_path) -> None:
    assert dotfiles.Repo(tmp_path / "a" / "dots").name != dotfiles.Repo(tmp_path / "b" / "dots").name


def test_a_learned_store_case_answers_its_own_store_only(tmp_path) -> None:
    case = census.Case(id=briefs.LEARNED + "x", match="error: failed", fix="true", repair="replay", store="aur")
    assert census.recognise([case], "error: failed", "aur") is case
    assert census.recognise([case], "error: failed", "flatpak") is None


def test_a_config_name_the_repository_ships_as_a_folder_is_a_folder_before_install(ground, tmp_path, monkeypatch) -> None:
    up, mine = ground
    commit(up, "ships a folder", **{"dots/.config/fuzzel/fuzzel.ini": "x=1\n"})
    git(up, "tag", "v1.2")
    config = tmp_path / "config"
    config.mkdir()
    (config / "fuzzel").write_text("I am a file\n")
    (config / "foot").symlink_to(tmp_path / "gone")  # a dead link the repository does not ship: left alone
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine, install=("test", "-d", str(config / "fuzzel"))))
    assert not outcome.case and (config / "fuzzel").is_dir()
    assert (config / "fuzzel.arch-update-auto-test").read_text() == "I am a file\n"
    assert (config / "foot").is_symlink() and any("fuzzel" in line for line in outcome.held)


def test_a_timeout_kills_a_command_that_ignores_sigterm(tmp_path, monkeypatch) -> None:
    from arch_updater import shell

    monkeypatch.setattr(shell, "KILL_AFTER", 1)
    started = time.monotonic()
    result = Shell().logged(["sh", "-c", "trap '' TERM; sleep 30"], tmp_path / "log", timeout=1)
    assert result.returncode == 124 and time.monotonic() - started < 10


def test_repositories_are_fetched_at_once_and_not_again(tmp_path) -> None:
    class SlowFetch(Shell):
        fetches = 0

        def capture(self, argv, timeout=30):
            if "fetch" in argv:
                type(self).fetches += 1
                time.sleep(1)
            return super().capture(argv, timeout)

    repos = []
    for name in ("a", "b"):
        path = tmp_path / name
        path.mkdir()
        git(path, "init", "-q")
        repos.append(dotfiles.Repo(path))
    world = World(tmp_path)
    world.sh = SlowFetch()
    started = time.monotonic()
    fetched = dotfiles.prefetch(world, repos)
    assert time.monotonic() - started < 1.8 and set(fetched) == {repo.path for repo in repos}
    dotfiles.update(world, repos[0], fetched[repos[0].path])
    assert SlowFetch.fetches == 2  # the update used the prefetched one


def test_what_your_edits_hold_back_is_shown_as_git_marks_it(ground, tmp_path) -> None:
    up, mine = ground
    (mine / "bar.conf").write_text("height=30\ncolor=black\n")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    found = outcome.drift
    assert not outcome.case and found.hunks == 1 and found.held_lines >= 1 and found.behind == 1
    hunk = found.held["bar.conf"][0]
    assert hunk.startswith("<<<<<<< ") and "\n||||||| " in hunk and "color=blue" in hunk and "color=black" in hunk
    report = (dotfiles.STATE_ROOT / "drift").glob("*.md")
    assert any("color=blue" in path.read_text() for path in report)
    assert any("held back by your edits" in line for line in outcome.held)


@pytest.mark.parametrize(("name", "good", "bad"), [
    ("tool.py", "x = 1\n", "x = (\n"),
    ("conf.json", '{"a": 1}\n', '{"a": 1,,}\n'),
    ("prompt.fish", "set x 1\n", "if true\n"),
    ("run.sh", "#!/bin/sh\necho hi\n", "#!/bin/sh\nif then\n"),
])
def test_an_upstream_change_that_does_not_parse_is_given_back_as_yours(ground, tmp_path, name, good, bad) -> None:
    up, mine = ground
    if name.endswith(".fish") and not shutil.which("fish"):
        pytest.skip("fish is not installed")
    commit(up, "add", **{name: good})
    git(up, "tag", "v1.1.1")
    git(mine, "fetch", "-q", "--tags")
    git(mine, "checkout", "-q", "v1.1.1")
    commit(up, "break it", **{name: bad, "other.txt": "fine\n"})
    git(up, "tag", "v1.2")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert not outcome.case and ancestor(mine, "v1.2") and (mine / "other.txt").is_file()
    assert (mine / name).read_text() == good and name in outcome.drift.unparsed
    assert not git(mine, "status", "--porcelain")


def test_a_file_that_was_already_broken_is_not_upstreams_doing(ground, tmp_path) -> None:
    up, mine = ground
    commit(up, "add", **{"tool.py": "x = (\n"})
    git(up, "tag", "v1.1.1")
    git(mine, "fetch", "-q", "--tags")
    git(mine, "checkout", "-q", "v1.1.1")
    commit(up, "still broken", **{"tool.py": "y = (\n"})
    git(up, "tag", "v1.2")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert (mine / "tool.py").read_text() == "y = (\n" and not outcome.drift.unparsed


@pytest.mark.parametrize(("suffix", "good", "bad", "tool"), [
    (".json", "[1]", "[1,", None),
    (".toml", "a = 1", "a = ", None),
    (".py", "a = 1", "a = (", None),
    (".sh", "echo hi", "if then", "sh"),
    (".bash", "echo hi", "if then", "bash"),
    (".fish", "echo hi", "if true", "fish"),
    (".qml", "import QtQuick\nItem { width: 3 }\n", "import QtQuick\nItem { width: \n", "qmlformat"),
    (".lua", "local a = 1", "local a = ", "luac"),
    (".ps1", "$a = 1", "$a = (", "pwsh"),
])
def test_each_kind_is_parsed_by_its_own_parser(tmp_path, suffix, good, bad, tool) -> None:
    if tool and not shutil.which(tool):
        pytest.skip(f"{tool} is not installed")
    (tmp_path / f"good{suffix}").write_text(good)
    (tmp_path / f"bad{suffix}").write_text(bad)
    assert syntax.check(tmp_path / f"good{suffix}") is None
    assert syntax.check(tmp_path / f"bad{suffix}") is not None


def test_a_kind_nothing_here_can_parse_is_never_called_broken(tmp_path) -> None:
    (tmp_path / "hyprland.conf").write_text("}}} not even close {{{")
    (tmp_path / "noshebang").write_text("if then")
    assert syntax.check(tmp_path / "hyprland.conf") is None and syntax.check(tmp_path / "noshebang") is None


def test_an_edit_hidden_by_assume_unchanged_stops_everything(ground, tmp_path) -> None:
    up, mine = ground
    (mine / "bar.conf").write_text("height=30\ncolor=mine\n")
    git(mine, "update-index", "--assume-unchanged", "bar.conf")
    head = git(mine, "rev-parse", "HEAD")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.case == "DOTFILES-UNSAVED" and "bar.conf" in outcome.output
    assert (mine / "bar.conf").read_text() == "height=30\ncolor=mine\n" and git(mine, "rev-parse", "HEAD") == head


def test_a_merge_the_person_left_open_is_not_committed_with_its_markers(ground, tmp_path) -> None:
    up, mine = ground
    git(mine, "checkout", "-q", "-b", "work")
    commit(mine, "mine", **{"bar.conf": "height=30\ncolor=mine\n"})
    git(mine, "fetch", "-q", "--tags")
    subprocess.run(["git", "-C", str(mine), *ID, "merge", "v1.1"], capture_output=True)  # conflicts, left open
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    assert outcome.case == "DOTFILES-UNSAVED" and "MERGE_HEAD" in outcome.output
    assert (mine / ".git" / "MERGE_HEAD").exists()  # the person's merge, untouched


def test_learn_refuses_a_fix_that_merges_nothing(ground, tmp_path) -> None:
    up, mine = ground
    deleted_upstream(up, mine, "old.conf", "v1.2")
    first = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    facts = {"target": first.target, "rollback": first.rollback}
    folder = briefs.write(dotfiles.Repo(mine).name, census.Case(id=first.case), first.output, "auto-test", None, str(mine), state_root=tmp_path, facts=facts)
    for abort in ("git merge --abort", "git reset -q --hard HEAD"):
        with pytest.raises(ValueError, match="not merged"):
            briefs.learn(folder, r"CONFLICT", "no", abort, state_root=tmp_path, followed={str(mine)})


def test_learn_runs_nothing_the_brief_folder_chooses(ground, tmp_path) -> None:
    up, mine = ground
    folder = briefs.write("dotfiles:x", census.Case(id="DOTFILES-CONFLICT"), "CONFLICT x\n", "auto-test", None, str(mine), state_root=tmp_path,
                          facts={"target": "refs/tags/v1.1", "rollback": "--upload-pack=touch pwned"})
    with pytest.raises(ValueError, match="not a repository"):
        briefs.learn(folder, "CONFLICT", "x", "true", state_root=tmp_path, followed=set())
    with pytest.raises(ValueError, match="plain ref"):
        briefs.learn(folder, "CONFLICT", "x", "true", state_root=tmp_path, followed={str(mine)})
    store = briefs.write("repo", census.Case(id="LOCAL-1"), "error: x\n", "auto-test", None, str(mine), state_root=tmp_path)
    with pytest.raises(ValueError, match="home folder"):
        briefs.learn(store, "error", "x", "true", state_root=tmp_path, followed=set())


def test_every_gh_login_is_yours(tmp_path, monkeypatch) -> None:
    from arch_updater import discover

    (tmp_path / ".config/gh").mkdir(parents=True)
    (tmp_path / ".config/gh/hosts.yml").write_text(
        "github.com:\n    users:\n        first:\n            oauth_token: x\n        second:\n            oauth_token: y\n"
        "    git_protocol: https\n    user: first\n"
    )
    monkeypatch.setattr(discover, "HOME", tmp_path)
    assert discover.own_logins() == {"first", "second"}


def test_every_plus_and_minus_is_watched_a_setting_upstream_also_sets_elsewhere(ground, tmp_path) -> None:
    """git merges these without a word: your `anim` in one file, upstream's new `anim` in a file read later."""
    up, mine = ground
    (mine / "old.conf").write_text("a\nanim = slow\n")
    (mine / "fx.conf").write_text("blur = on\n")
    commit(up, "late file", **{"late.conf": "anim = fast\n", "fx.conf": "blur = on\n"})
    git(up, "tag", "v1.2")
    outcome = dotfiles.update(World(tmp_path), dotfiles.Repo(mine))
    found = outcome.drift
    assert not outcome.case and (mine / "old.conf").read_text() == "a\nanim = slow\n"
    assert any(line.startswith("anim: yours `anim = slow`") and "late.conf" in line and "anim = fast" in line for line in found.shadowed)
    assert any("blur = on" in line for line in found.adopted)  # upstream made your change itself
    report = next((dotfiles.STATE_ROOT / "drift").glob("*.md")).read_text()
    assert "anim = fast" in report and "Your lines upstream has now" in report


def test_a_list_like_key_is_not_a_setting() -> None:
    from arch_updater import drift

    yours = "+++ b/k.conf\n@@ -0,0 +1 @@\n+bind = SUPER, Q, exec, kitty\n"
    theirs = "+++ b/u.conf\n@@ -0,0 +3 @@\n+bind = A\n+bind = B\n+bind = C\n"
    assert drift._settings(yours, theirs) == ([], [])
