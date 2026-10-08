"""What else this account installed from git, offered to be followed. Read-only: it never clones or installs.

Two kinds. A clone with an outside upstream and an installer at its root that is
not under "dotfiles" yet. And a config folder that was copied, not cloned, whose
files name the repository it came from (a README, a LICENSE, links in comments):
its upstream is recovered from those links. Each comes back as one line and the
command that follows it; nothing is added without that command, because an
installer from upstream runs as the account and nothing here can vouch for it.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .engine import STATE_ROOT

HOME = Path.home()
# Where people keep clones of desktop setups, and how deep to look.
CLONE_ROOTS = ((HOME, 1), (HOME / ".config", 2), (HOME / ".local/share", 2), (HOME / ".cache", 1), (HOME / "src", 1), (HOME / "git", 1), (HOME / "repos", 1))
INSTALLERS = ("setup", "install.sh", "install", "setup.sh", "bootstrap.sh")
HOSTS = ("github.com", "gitlab.com", "codeberg.org")
URL = re.compile(r"https?://(?:www\.)?(github\.com|gitlab\.com|codeberg\.org)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[/#\s\"'<>)\]]|$)")
# Files that tell where a folder came from, read first and only these: no file is executed or parsed as code.
TELLERS = ("README.md", "README", "readme.md", "LICENSE", "LICENSE.md", "COPYING", "welcome.qml", "shell.qml", "config.jsonc", "package.json")
NOT_UPSTREAM = {"issues", "releases", "wiki", "blob", "tree", "sponsors"}
# What a known upstream's installer is when nobody sits at the terminal.
RECIPES = {"github.com/end-4/dots-hyprland": "./setup install-files -f"}
NOTIFY_EVERY_DAYS = 30
SEEN = "discover.json"


@dataclass(frozen=True)
class Offer:
    kind: str  # clone | copied
    path: str
    upstream: str  # host/owner/repo
    install: str
    why: str

    @property
    def command(self) -> str:
        if self.kind == "clone":
            return f"arch-update dotfiles --add {self.path}" + (f" --install '{self.install}'" if self.install else "")
        return f"git clone https://{self.upstream} ~/.local/share/dotfiles/{self.upstream.rsplit('/', 1)[-1]}  # then: arch-update dotfiles --add <that folder>"


def _origin(path: Path) -> str:
    done = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def _upstream(url: str) -> str:
    found = URL.search(url if "://" in url else "https://" + url.replace(":", "/", 1).split("@", 1)[-1])
    return f"{found[1]}/{found[2]}/{found[3]}".lower() if found else ""


def own_logins() -> set[str]:
    """The account's own logins (gh's hosts.yml): its own repositories are written here, not followed."""
    try:
        text = (HOME / ".config/gh/hosts.yml").read_text(encoding="utf-8")
    except OSError:
        return set()
    # `user:` is the active login; every login gh knows is a key under `users:` (two accounts: both are yours).
    found = set(re.findall(r"^\s+user:\s*(\S+)", text, re.MULTILINE))
    for block in re.findall(r"^(\s+)users:\s*\n((?:\1\s+.*\n?)*)", text, re.MULTILINE):
        indent = min((len(line) - len(line.lstrip()) for line in block[1].splitlines() if line.strip()), default=0)
        found.update(line.strip()[:-1] for line in block[1].splitlines() if line.strip().endswith(":") and len(line) - len(line.lstrip()) == indent)
    return {login.lower() for login in found}


def _clones(followed: set[str], mine: set[str]) -> list[Offer]:
    offers = []
    for root, depth in CLONE_ROOTS:
        for git in _walk(root, depth):
            path = git.parent
            upstream = _upstream(_origin(path))
            if not upstream or str(path) in followed or upstream.split("/")[1] in mine:
                continue
            installer = next((name for name in INSTALLERS if (path / name).is_file()), "")
            if not installer:
                continue  # a library or a project, not a desktop setup
            install = RECIPES.get(upstream, f"./{installer}")
            offers.append(Offer("clone", str(path), upstream, install, f"a clone of {upstream} with ./{installer}, not followed"))
    return offers


def _walk(root: Path, depth: int) -> list[Path]:
    found: list[Path] = []
    try:
        children = [child for child in root.iterdir() if child.is_dir() and not child.is_symlink()]
    except OSError:
        return found
    for child in children:
        if (child / ".git").exists():
            found.append(child / ".git")
        elif depth > 1:
            found += _walk(child, depth - 1)
    return found


def _copied(followed_upstreams: set[str]) -> list[Offer]:
    """Config folders that are not clones of their upstream, but say where they came from."""
    offers = []
    config = HOME / ".config"
    try:
        folders = [path for path in config.iterdir() if path.is_dir() and not path.is_symlink()]
    except OSError:
        return offers
    for folder in folders:
        votes: Counter[str] = Counter()
        for child in [folder, *(path for path in folder.iterdir() if path.is_dir() and not path.is_symlink())][:40]:
            for name in TELLERS:
                path = child / name
                try:
                    if not path.is_file():
                        continue
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        text = handle.read(200_000)  # the head is enough, whatever the file (or a link) weighs
                except OSError:
                    continue
                for host, owner, repo in URL.findall(text):
                    if repo.lower() not in NOT_UPSTREAM and owner.lower() not in NOT_UPSTREAM:
                        votes[f"{host}/{owner}/{repo}".lower()] += 1
        if not votes:
            continue
        upstream, count = votes.most_common(1)[0]
        own = _upstream(_origin(folder))
        if upstream in followed_upstreams or own == upstream or count < 2:
            continue  # followed already, a clone of it, or one stray link
        why = (f"{folder.name} names {upstream} {count} times, but is not a clone of it. Following it means its installer"
               f" copies upstream's files over this folder: your own changes here are not in that clone")
        offers.append(Offer("copied", str(folder), upstream, RECIPES.get(upstream, ""), why))
    return offers


def offers(config: dict[str, Any] | None) -> list[Offer]:
    from .dotfiles import repos

    followed = repos(config)
    paths = {str(repo.path) for repo in followed}
    upstreams = {_upstream(_origin(repo.path)) for repo in followed}
    found = _clones(paths, own_logins()) + _copied(upstreams)
    return sorted({offer.path: offer for offer in found}.values(), key=lambda offer: offer.path)


def news(found: list[Offer], state_root: Path | None = None, now: float | None = None) -> list[Offer]:
    """The offers worth a notice: never shown, or last shown a month ago. Shows them, so the next call is quiet."""
    path = (state_root or STATE_ROOT) / SEEN
    now = now or time.time()
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    fresh = [offer for offer in found if now - float(seen.get(offer.path, 0)) >= NOTIFY_EVERY_DAYS * 86400]
    for offer in fresh:
        seen[offer.path] = now
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")
    return fresh


def as_dicts(found: list[Offer]) -> list[dict[str, str]]:
    return [{**asdict(offer), "command": offer.command} for offer in found]
