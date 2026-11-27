"""The README names the same version the package reports."""

from __future__ import annotations

from pathlib import Path

from arch_updater import __version__

README = Path(__file__).resolve().parents[1] / "README.md"


def test_the_readme_names_the_package_version() -> None:
    assert f"Version {__version__}." in README.read_text(encoding="utf-8")
