"""Does this config file still parse? One parser per kind, the program's own where it has one.

Used after a dotfiles merge: a file upstream changed that no longer parses, while
the person's own version did, is given back to the person (dotfiles.py). Only
parsing is checked, never running: `bash -n`, `fish --no-execute`, the Python
parser, `qmlformat`, `luac -p`, the PowerShell parser, JSON, TOML. A kind with no
parser here, or whose parser is not installed, has no verdict: it is never
called broken on a guess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

TIMEOUT = 30
# The path comes in through the environment: pasted into the command it would be code, whatever its name says.
PS_PARSE = (
    "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($env:ARCH_UPDATE_PARSE,[ref]$null,[ref]$e); "
    "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }"
)
PY_PARSE = "import ast, sys; ast.parse(open(sys.argv[1], 'rb').read(), sys.argv[1])"
# By suffix; a file without one is known by its #! line.
BY_SUFFIX = {
    ".sh": ("sh", "-n"),
    ".bash": ("bash", "-n"),
    ".zsh": ("zsh", "-n"),
    ".fish": ("fish", "--no-execute"),
    ".py": (sys.executable, "-I", "-c", PY_PARSE),
    ".qml": ("qmlformat",),
    ".lua": ("luac", "-p", "-o", "/dev/null"),
    ".ps1": ("pwsh", "-NoProfile", "-NonInteractive", "-Command", PS_PARSE),
    ".psm1": ("pwsh", "-NoProfile", "-NonInteractive", "-Command", PS_PARSE),
}
BY_SHEBANG = {"sh": ".sh", "bash": ".bash", "zsh": ".zsh", "fish": ".fish", "python": ".py", "python3": ".py", "pwsh": ".ps1"}


def kind(path: Path) -> str:
    """The suffix that decides the parser: the file's own, or the one its #! line names."""
    suffix = path.suffix.lower()
    if suffix in BY_SUFFIX or suffix in (".json", ".toml"):
        return suffix
    try:
        first = path.open("rb").readline(200).decode("utf-8", "replace")
    except OSError:
        return ""
    if not first.startswith("#!"):
        return ""
    words = first[2:].split()
    program = Path(words[1] if words and Path(words[0]).name == "env" and len(words) > 1 else (words[0] if words else "")).name
    return BY_SHEBANG.get(program, "")


def check(path: Path, as_kind: str | None = None) -> str | None:
    """The parser's complaint about the file, or None when it parses or nothing here can tell."""
    found = as_kind or kind(path)
    try:
        if found == ".json":
            json.loads(path.read_text(encoding="utf-8"))
            return None
        if found == ".toml":
            tomllib.loads(path.read_text(encoding="utf-8"))
            return None
    except (ValueError, UnicodeDecodeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    except OSError:
        return None
    argv = BY_SUFFIX.get(found)
    if not argv or not shutil.which(argv[0]):
        return None
    by_env = argv[0] == "pwsh"
    env = {**os.environ, "ARCH_UPDATE_PARSE": str(path)} if by_env else None
    try:
        done = subprocess.run(
            [*argv] if by_env else [*argv, str(path)],
            capture_output=True, text=True, errors="replace", timeout=TIMEOUT, check=False, stdin=subprocess.DEVNULL, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if done.returncode == 0 else ((done.stderr or done.stdout).strip()[-500:] or f"{argv[0]} exit {done.returncode}")
