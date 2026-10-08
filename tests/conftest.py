"""No test may touch the real home: state, config and cache all live under a temporary directory."""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
import tempfile
from pathlib import Path

import pytest

# The real files, remembered before the environment is moved, so a test can prove they stay untouched.
REAL_HOME = Path(os.path.expanduser("~"))
REAL_STATE = Path(os.environ.get("ARCH_UPDATER_STATE", REAL_HOME / ".local/state/arch-updater"))

# Module-level paths (STATE_ROOT and friends) are computed at import, so the session starts out of the real home too.
_SESSION = Path(tempfile.mkdtemp(prefix="arch-updater-tests-"))
for _name, _sub in (("HOME", ""), ("XDG_STATE_HOME", ".local/state"), ("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache")):
    os.environ[_name] = str(_SESSION / _sub)
os.environ["ARCH_UPDATER_STATE"] = str(_SESSION / ".local/state/arch-updater")

import arch_updater  # noqa: E402

for _info in pkgutil.iter_modules(arch_updater.__path__):
    importlib.import_module(f"arch_updater.{_info.name}")


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = home / ".local/state/arch-updater"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("ARCH_UPDATER_STATE", str(state))
    # No test may reach the real sudo: on a machine with the unattended rules installed, `sudo -n pacman -Syu`
    # needs no password, and a test once updated this machine through it (2026-10-08). The first sudo on PATH is this.
    guard = tmp_path / "no-sudo"
    guard.mkdir()
    (guard / "sudo").write_text("#!/bin/sh\necho 'tests may not run sudo' >&2\nexit 97\n")
    (guard / "sudo").chmod(0o755)
    # systemctl may be read (show, list-timers), never told to change a real unit.
    (guard / "systemctl").write_text(
        "#!/bin/sh\nfor word in \"$@\"; do case \"$word\" in set-property|start|stop|restart|enable|disable|daemon-reload|kill|mask)"
        " echo \"tests may not run systemctl $word\" >&2; exit 97;; esac; done\nexec /usr/bin/systemctl \"$@\"\n"
    )
    (guard / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}:{os.environ.get('PATH', '')}")
    # The quiet gate reads the live machine (a game, pressure, battery): a test sees an empty one instead.
    from arch_updater import quiet

    (tmp_path / "proc").mkdir()
    (tmp_path / "sys").mkdir()
    monkeypatch.setattr(quiet, "PROC", tmp_path / "proc")
    monkeypatch.setattr(quiet, "SYS", tmp_path / "sys")
    for name, module in list(sys.modules.items()):
        if not name.startswith("arch_updater."):
            continue
        if hasattr(module, "STATE_ROOT"):
            monkeypatch.setattr(module, "STATE_ROOT", state)
        if hasattr(module, "HISTORY_PATH"):
            monkeypatch.setattr(module, "HISTORY_PATH", state / "patrol-history.json")
        if name == "arch_updater.auto":  # the machine's own config; engine.CONFIG_PATH is the shipped profile list
            monkeypatch.setattr(module, "CONFIG_PATH", home / ".config/arch-updater/auto.json")
    yield home
