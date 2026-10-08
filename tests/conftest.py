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
