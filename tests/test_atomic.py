from pathlib import Path

import pytest

from arch_updater import atomic


def test_a_write_that_dies_before_the_swap_leaves_the_old_file_whole(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "w"
    folder.mkdir()
    target = folder / "maintainers.json"
    target.write_text('{"pkg": "someone"}')

    def die(self, destination):
        raise OSError("power cut")

    monkeypatch.setattr(Path, "replace", die)
    with pytest.raises(OSError):
        atomic.write(target, '{"pkg": "other"}')
    assert target.read_text() == '{"pkg": "someone"}', "the old record must survive a failed swap"
    assert [p.name for p in folder.iterdir()] == ["maintainers.json"], "no half-written file may be left behind"


def test_a_write_makes_the_folder_and_replaces_the_file(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "state.json"
    atomic.write(target, "one")
    atomic.write(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]
