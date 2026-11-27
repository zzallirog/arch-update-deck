"""The command script runs the installed package, never one in the folder it was started from."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "arch-update"


def _package(folder: Path, says: str) -> None:
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text("")
    (folder / "__main__.py").write_text(f"print({says!r})\n")


def test_a_package_in_the_working_folder_does_not_replace_the_installed_one(tmp_path) -> None:
    _package(tmp_path / "share" / "arch_updater", "installed")
    _package(tmp_path / "work" / "arch_updater", "working folder")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["ARCH_UPDATER_HOME"] = str(tmp_path / "share")
    done = subprocess.run(["sh", str(LAUNCHER), "--version"], cwd=tmp_path / "work", env=env, capture_output=True, text=True, timeout=60)
    assert done.stdout == "installed\n", done.stderr
