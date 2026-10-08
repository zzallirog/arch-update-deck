"""The root shim: what it lets through and what it refuses, on package files built here."""

from __future__ import annotations

import io
import json
import tarfile
import time

import pytest

from arch_updater import aur_handoff, aur_root, stores

PKGINFO = "pkgname = {name}\npkgver = {version}\n{extra}"


def package(path, *members, name="app-bin", version="2-1", extra=""):
    """Write a .pkg.tar.zst: .PKGINFO first, then (name, kind, options) members."""
    with tarfile.open(path, "w:zst", format=tarfile.PAX_FORMAT) as archive:
        entries = [(".PKGINFO", "file", {"data": PKGINFO.format(name=name, version=version, extra=extra).encode()}), *members]
        for member_name, kind, options in entries:
            info = tarfile.TarInfo(member_name)
            info.uid = info.gid = options.get("uid", 0)
            info.mode = options.get("mode", 0o755 if kind == "dir" else 0o644)
            info.pax_headers = options.get("pax", {})
            data = options.get("data", b"")
            if kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, options["to"]
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, options["to"]
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
            info.size = len(data) if kind == "file" else 0
            archive.addfile(info, io.BytesIO(data) if kind == "file" else None)
    return path


INSTALLED = {"app-bin": {"version": "1-1", "conflicts": [], "replaces": [], "provides": ["app"], "paths": ["/usr/lib/app-data/x"]}}
GOOD = [
    ("opt", "dir", {}),
    ("opt/app", "dir", {}),
    ("opt/app/app", "file", {"mode": 0o755, "data": b"\x7fELF"}),
    ("usr", "dir", {}),
    ("usr/bin", "dir", {}),
    ("usr/bin/app", "link", {"to": "/opt/app/app"}),
    ("usr/share", "dir", {}),
    ("usr/share/applications/app.desktop", "file", {}),
    ("usr/lib/app-data/x", "file", {}),
    (".MTREE", "file", {}),
]


def test_an_ordinary_bin_package_update_passes(tmp_path) -> None:
    assert aur_root.inspect(package(tmp_path / "a.pkg.tar.zst", *GOOD), INSTALLED) == {"name": "app-bin", "version": "2-1"}


def test_a_rebuild_of_the_installed_version_passes_and_an_older_one_does_not(tmp_path) -> None:
    assert aur_root.inspect(package(tmp_path / "a.pkg.tar.zst", *GOOD, version="1-1"), INSTALLED)["version"] == "1-1"
    with pytest.raises(aur_root.Refused, match="older than the installed"):
        aur_root.inspect(package(tmp_path / "b.pkg.tar.zst", *GOOD, version="0.9-1"), INSTALLED)


ATTACKS = {
    "install script": ([(".INSTALL", "file", {"data": b"post_upgrade() { id; }"})], {}, "install script"),
    "unknown metadata": ([(".HOOK", "file", {})], {}, "unknown metadata"),
    "etc": ([("etc/sudoers.d/x", "file", {})], {}, "outside the allowed places"),
    "systemd unit": ([("usr/lib/systemd/system/x.service", "file", {})], {}, "outside the allowed places"),
    "pacman hook": ([("usr/share/libalpm/hooks/x.hook", "file", {})], {}, "outside the allowed places"),
    "top-level library": ([("usr/lib/libc.so.7", "file", {})], {}, "outside the allowed places"),
    "named like a system folder": ([("usr/lib/security/pam_x.so", "file", {})], {"name": "security"}, "outside the allowed places"),
    "setuid": ([("usr/bin/app2", "file", {"mode": 0o4755})], {}, "setuid"),
    "setgid folder": ([("opt/app2", "dir", {"mode": 0o2755})], {}, "setuid or setgid"),
    "not root's": ([("usr/bin/app2", "file", {"uid": 1000})], {}, "not owned by root"),
    "world-writable": ([("opt/app/w", "file", {"mode": 0o666})], {}, "writable by anyone"),
    "file capability": ([("usr/bin/app2", "file", {"pax": {"SCHILY.xattr.security.capability": "\x01"}})], {}, "extended attributes"),
    "hard link": ([("usr/bin/app2", "hard", {"to": "etc/shadow"})], {}, "hard link"),
    "fifo": ([("opt/app/p", "fifo", {})], {}, "special file"),
    "through its own link": ([("opt/etc", "link", {"to": "/etc"}), ("opt/etc/sudoers.d/x", "file", {})], {}, "goes through a link"),
    "link to etc": ([("usr/bin/conf", "link", {"to": "/etc/shadow"})], {}, "points to"),
    "absolute path": ([("/etc/x", "file", {})], {}, "bad path"),
    "climbs out": ([("opt/../etc/x", "file", {})], {}, "bad path"),
    "new conflict": ([], {"extra": "conflict = sudo\n"}, "adds conflict sudo"),
    "new replacement": ([], {"extra": "replaces = sudo\n"}, "adds replaces sudo"),
    "new provision": ([], {"extra": "provides = sudo\n"}, "adds provides sudo"),
}


@pytest.mark.parametrize("attack", ATTACKS, ids=str)
def test_an_attack_is_refused_with_its_reason(tmp_path, attack) -> None:
    members, options, reason = ATTACKS[attack]
    path = package(tmp_path / "a.pkg.tar.zst", *GOOD, *members, **options)
    with pytest.raises(aur_root.Refused, match=reason):
        aur_root.inspect(path, {**INSTALLED, options.get("name", "app-bin"): INSTALLED["app-bin"]})


def test_a_link_already_on_disk_is_not_a_way_in(tmp_path, monkeypatch) -> None:
    real = aur_root.os.path.realpath
    monkeypatch.setattr(aur_root.os.path, "realpath", lambda path: "/usr/lib/systemd/system" if path == "/opt/app/units" else real(path))
    path = package(tmp_path / "a.pkg.tar.zst", *GOOD, ("opt/app/units/evil.service", "file", {}))
    with pytest.raises(aur_root.Refused, match="link already on disk"):
        aur_root.inspect(path, INSTALLED)


def test_a_link_may_point_at_a_system_file_but_not_a_system_folder(tmp_path) -> None:
    assert aur_root.inspect(package(tmp_path / "a.pkg.tar.zst", *GOOD, ("opt/app/libc", "link", {"to": "/usr/lib/libc.so.6"})), INSTALLED)
    with pytest.raises(aur_root.Refused, match="points to"):
        aur_root.inspect(package(tmp_path / "b.pkg.tar.zst", *GOOD, ("opt/app/units", "link", {"to": "/usr/lib/systemd/system"})), INSTALLED)


def test_only_an_installed_foreign_package_is_replaced(tmp_path) -> None:
    with pytest.raises(aur_root.Refused, match="not an installed foreign package"):
        aur_root.inspect(package(tmp_path / "a.pkg.tar.zst", *GOOD, name="sudo"), INSTALLED)


def test_something_that_is_not_a_package_is_refused(tmp_path) -> None:
    (tmp_path / "a.pkg.tar.zst").write_bytes(b"not zstd")
    with pytest.raises(aur_root.Refused, match="not a readable package"):
        aur_root.inspect(tmp_path / "a.pkg.tar.zst", INSTALLED)


def test_the_shim_refuses_to_run_with_arguments_or_without_root(monkeypatch, capsys) -> None:
    monkeypatch.setattr(aur_root.sys, "argv", ["shim", "--config", "/tmp/evil.conf"])
    monkeypatch.setattr(aur_root.os, "geteuid", lambda: 0)
    assert aur_root.main() == 64
    monkeypatch.setattr(aur_root.sys, "argv", ["shim"])
    monkeypatch.setattr(aur_root.os, "geteuid", lambda: 1000)
    assert aur_root.main() == 64


def test_the_shim_copies_without_following_a_link(tmp_path) -> None:
    stage, into = tmp_path / "stage", tmp_path / "root"
    stage.mkdir(), into.mkdir()
    (stage / "x.pkg.tar.zst").symlink_to("/etc/hostname")
    fd = aur_root.os.open(stage, aur_root.os.O_RDONLY | aur_root.os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            aur_root.take(fd, "x.pkg.tar.zst", into)
    finally:
        aur_root.os.close(fd)
    assert not list(into.iterdir())


# ── the hand-over yay runs instead of sudo ──────────────────────────────────


class Hand:
    def __init__(self, results):
        self.results, self.typed = results, []

    def post(self, url, fields):
        return {"type": "multiinfo", "results": self.results}

    def interactive(self, argv):
        self.typed.append(argv)
        return 0


@pytest.fixture
def hand(tmp_path, monkeypatch):
    monkeypatch.setattr(aur_handoff, "STAGE", tmp_path / "stage")
    monkeypatch.setattr(aur_root, "installed_foreign", lambda: INSTALLED)
    shim = tmp_path / "shim"
    shim.write_bytes(aur_handoff.Path(aur_root.__file__).read_bytes())
    monkeypatch.setattr(aur_handoff, "SHIM", shim)
    return tmp_path


def test_the_hand_over_stages_checked_files_and_runs_only_the_shim(hand) -> None:
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    sh = Hand([{"Name": "app-bin", "LastModified": time.time() - 30 * 86400}])
    seen = []
    sh.interactive = lambda argv: seen.append((argv, sorted(p.name for p in (hand / "stage").iterdir()))) or 0
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(built)], 7, sh) == 0
    assert seen == [(["sudo", "-n", str(aur_handoff.SHIM)], [built.name])]
    assert not list((hand / "stage").iterdir()), "the stage is emptied after the shim"


@pytest.mark.parametrize("argv", [["pacman", "-S", "--asdeps", "--", "x"], ["pacman", "-U", "--overwrite", "*", "--", "x"], ["pacman", "-U", "--noconfirm", "--config", "/etc/pacman.conf", "--"], ["sh", "-c", "id"]])
def test_the_hand_over_refuses_anything_but_a_plain_install(hand, argv) -> None:
    sh = Hand([])
    assert aur_handoff.handoff(argv, 7, sh) == 1 and sh.typed == []


def test_reason_marks_are_answered_without_root(hand) -> None:
    sh = Hand([])
    assert aur_handoff.handoff(["pacman", "-D", "-q", "--asexplicit", "--noconfirm", "--config", "/etc/pacman.conf", "--", "x"], 7, sh) == 0 and sh.typed == []


def test_an_entry_that_changed_while_building_is_not_installed(hand) -> None:
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    sh = Hand([{"Name": "app-bin", "LastModified": time.time() - 60}])
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(built)], 7, sh) == 1 and sh.typed == []


def test_a_shim_older_than_the_release_is_not_run(hand) -> None:
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    (hand / "shim").write_text("#!/usr/bin/python3\n# last release\n")
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(built)], 7, sh) == 1 and sh.typed == []


def test_a_refused_package_never_reaches_sudo(hand) -> None:
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD, (".INSTALL", "file", {}))
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(built)], 7, sh) == 1 and sh.typed == []


# ── which waiting updates are never built unasked ───────────────────────────


class Look:
    def __init__(self, installed, results):
        self.installed, self.results = installed, results

    def capture(self, argv, timeout=30):
        from arch_updater.engine import CommandResult

        if argv == ["pacman", "-Qqm"]:
            return CommandResult(argv, 0, "\n".join(self.installed) + "\n")
        if argv == ["pacman", "-Qm"]:
            return CommandResult(argv, 0, "\n".join(f"{n} {v}" for n, v in self.installed.items()) + "\n")
        if argv[0] == "vercmp":
            return CommandResult(argv, 0, "1" if argv[1] != argv[2] else "0")
        raise AssertionError(argv)

    def post(self, url, fields):
        return {"type": "multiinfo", "results": self.results}


def entry(name, version="2", maintainer="alice", age_days=30, base=None):
    return {"Name": name, "PackageBase": base or name, "Version": version, "Maintainer": maintainer, "LastModified": time.time() - age_days * 86400}


def test_vcs_orphans_and_new_maintainers_wait_for_a_person(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    state.write_text(json.dumps({"taken": "alice"}))
    look = Look({"ok": "1", "foo-git": "1", "orphan": "1", "taken": "1", "same": "2"}, [entry("ok"), entry("foo-git"), entry("orphan", maintainer=None), entry("taken", maintainer="mallory"), entry("same", maintainer="bob")])
    fresh, unsafe = stores.aur_review(look, 7, state)
    assert fresh == []
    assert unsafe == ["foo-git: VCS package, builds whatever upstream has now", "orphan: orphaned in the AUR", "taken: maintainer changed from alice to mallory"]
    assert json.loads(state.read_text()) == {"foo-git": "alice", "ok": "alice", "orphan": None, "same": "bob", "taken": "alice"}


def test_an_update_by_hand_accepts_the_new_maintainer(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    state.write_text(json.dumps({"taken": "alice"}))
    stores.aur_review(Look({"taken": "2"}, [entry("taken", maintainer="mallory")]), 7, state)
    assert json.loads(state.read_text()) == {"taken": "mallory"}


def test_an_adopted_orphan_is_a_maintainer_change(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    stores.aur_review(Look({"x": "1"}, [entry("x", maintainer=None)]), 7, state)
    assert stores.aur_review(Look({"x": "1"}, [entry("x", maintainer="mallory")]), 7, state)[1] == ["x: maintainer changed from None to mallory"]


def test_the_cool_down_is_a_week_unless_configured() -> None:
    assert stores.AUR_MIN_AGE_DAYS == 7
