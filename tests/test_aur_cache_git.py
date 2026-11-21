"""A yay cache folder with a damaged `.git` must not make git work in the repository of the home folder."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from arch_updater import census, engine, repairs, witness

FETCH_FAILURE = """\
:: (0/3) Failed to download PKGBUILD: r8152-dkms
:: (0/3) Failed to download PKGBUILD: binder_linux-dkms
 -> error fetching r8152-dkms: fatal: Cannot rebase onto multiple branches.
\t context: exit status 128

error fetching binder_linux-dkms: fatal: Cannot rebase onto multiple branches.
\t context: exit status 128
"""


def git(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", *argv], cwd=cwd, env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def home_repo(tmp_path: Path, monkeypatch) -> Path:
    """A home folder that is itself a repository (like a dotfiles home), with a yay cache under it."""
    home = tmp_path / "home"
    (home / ".cache" / "yay").mkdir(parents=True)
    assert git("init", "-q", str(home)).returncode == 0
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    return home


def husk(root: Path, name: str) -> Path:
    """The folder the 16th of November run met: a `.git` with an index and logs, no HEAD, no objects, no config."""
    folder = root / "yay" / name
    (folder / ".git" / "logs").mkdir(parents=True)
    (folder / ".git" / "refs").mkdir()
    (folder / ".git" / "index").write_bytes(b"DIRC")
    (folder / f"{name}-1-any.pkg.tar.zst").write_bytes(b"x")
    return folder


def test_without_a_ceiling_git_in_a_damaged_cache_folder_is_git_in_the_home_repository(home_repo) -> None:
    folder = husk(home_repo / ".cache", "binder_linux-dkms")
    found = git("rev-parse", "--show-toplevel", cwd=folder)
    assert found.stdout.strip() == str(home_repo.resolve()), "the failure this module guards against"


def test_a_helper_runs_with_git_kept_inside_the_cache_home(home_repo) -> None:
    folder = husk(home_repo / ".cache", "binder_linux-dkms")
    env = engine.environment(["yay", "-Sua"])
    assert env is not None
    done = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], env=env, capture_output=True, text=True, check=False)
    assert done.returncode != 0 and str(home_repo) not in done.stdout


def test_a_ceiling_already_set_by_the_person_is_kept(home_repo, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/srv")
    env = engine.environment(["yay", "-Sua"])
    assert env is not None
    assert env["GIT_CEILING_DIRECTORIES"].split(os.pathsep) == ["/srv", str(home_repo / ".cache")]


@pytest.mark.parametrize("argv", [["pacman", "-Syu"], ["sudo", "-n", "pacman", "-Syu"], ["flatpak", "update"]])
def test_other_programs_get_no_ceiling(argv, home_repo) -> None:
    env = engine.environment(argv)
    assert env is None or "GIT_CEILING_DIRECTORIES" not in env


def test_only_the_damaged_folders_are_set_aside_and_nothing_is_deleted(home_repo) -> None:
    cache = home_repo / ".cache"
    a, b = husk(cache, "r8152-dkms"), husk(cache, "binder_linux-dkms")
    sound = cache / "yay" / "anydesk-bin"
    sound.mkdir()
    assert git("init", "-q", str(sound)).returncode == 0
    moved = engine.quarantine_broken_aur_caches(FETCH_FAILURE + "\n -> error fetching anydesk-bin: fatal: Cannot rebase onto multiple branches.\n", "run-1")
    assert sorted(item["package"] for item in moved) == ["binder_linux-dkms", "r8152-dkms"]
    assert not a.exists() and not b.exists()
    assert (cache / "yay" / "r8152-dkms.quarantine-run-1" / "r8152-dkms-1-any.pkg.tar.zst").is_file(), "the package archive is kept"
    assert sound.is_dir(), "a folder where git finds a repository is not touched"


def test_a_name_that_is_not_a_direct_child_or_is_a_link_is_left_alone(home_repo, tmp_path) -> None:
    cache = home_repo / ".cache"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (cache / "yay" / "linked").symlink_to(elsewhere)
    out = "error fetching linked: fatal: Cannot rebase onto multiple branches.\nerror fetching ../escape: fatal: Cannot rebase onto multiple branches.\n"
    assert engine.quarantine_broken_aur_caches(out, "run-1") == []
    assert elsewhere.is_dir()


def test_the_repair_is_registered_and_the_catalogue_names_the_failure() -> None:
    assert "quarantine-broken-aur-cache" in repairs.ACTIONS
    case = next(item for item in census.load(Path("/nonexistent")) if item.id == "AUR-CACHE-GIT-DAMAGED")
    assert case.matches(FETCH_FAILURE) and case.repair == "quarantine-broken-aur-cache"
    assert not case.matches("error fetching foo: fatal: unable to access 'https://aur.archlinux.org/foo.git/'")


def test_a_pacman_warning_is_not_read_as_the_failure() -> None:
    warning = "warning: hyprland-0.57.2-1 is up to date -- reinstalling\n"
    assert witness.generalize(warning, ["hyprland"]) is None
    both = warning + ":: hyprland-0.57.2-1 and hyprland-git-1-1 are in conflict. Remove hyprland-git? [y/N]\n"
    general = witness.generalize(both, ["hyprland", "hyprland-git"])
    assert general is not None and "reinstalling" not in general.match and "conflict" in general.match
