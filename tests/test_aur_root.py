"""The root shim: what it lets through and what it refuses, on package files built here."""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

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


def test_the_held_packages_yay_passes_on_do_not_refuse_the_install(hand) -> None:
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    sh = Hand([{"Name": "app-bin", "LastModified": time.time() - 30 * 86400}])
    seen = []
    sh.interactive = lambda argv: seen.append(argv) or 0
    argv = ["pacman", "-U", "--ignore", "darkly-bin", "--ignore=zapzap", *aur_handoff.INSTALL[2:], str(built)]
    assert aur_handoff.handoff(argv, 7, sh) == 0
    assert seen == [["sudo", "-n", str(aur_handoff.SHIM)]]


def test_a_refused_package_is_set_aside_and_the_rest_still_installs(hand, monkeypatch) -> None:
    learned = hand / "aur-by-hand.json"
    monkeypatch.setattr(aur_handoff, "LEARNED", learned)
    monkeypatch.setattr(aur_root, "installed_foreign", lambda: {**INSTALLED, "style": {**INSTALLED["app-bin"], "paths": []}})
    good = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    plugin = package(hand / "style-2-1-x86_64.pkg.tar.zst", ("usr/lib/qt6/plugins/style.so", "file", {}), name="style")
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    seen = []
    sh.interactive = lambda argv: seen.append(sorted(p.name for p in (hand / "stage").iterdir())) or 0
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(plugin), str(good)], 7, sh) == 0
    assert seen == [[good.name]], "only the package that passed reaches the shim"
    assert json.loads(learned.read_text()) == {"style": {"why": "the shim refused it: usr/lib/qt6/plugins/style.so is outside the allowed places", "version": "2-1"}}


def test_a_file_that_is_not_a_package_is_set_aside_for_this_run_only(hand, monkeypatch) -> None:
    learned = hand / "aur-by-hand.json"
    monkeypatch.setattr(aur_handoff, "LEARNED", learned)
    broken = hand / "junk-2-1-x86_64.pkg.tar.zst"
    broken.write_bytes(b"not an archive")
    good = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    sh.interactive = lambda argv: 0
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(broken), str(good)], 7, sh) == 0
    assert not learned.exists(), "a refusal that names a file, not a package, is not remembered"


STYLE = [("opt", "dir", {}), ("opt/style", "dir", {}), ("opt/style/style", "file", {"mode": 0o755, "data": b"\x7fELF"})]


def test_a_package_the_shim_would_not_stage_is_set_aside_before_it_sinks_the_batch(hand, monkeypatch) -> None:
    learned = hand / "aur-by-hand.json"
    monkeypatch.setattr(aur_handoff, "LEARNED", learned)
    monkeypatch.setattr(aur_root, "installed_foreign", lambda: {**INSTALLED, "style": {**INSTALLED["app-bin"], "paths": []}})
    good = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    epoch = package(hand / "style-1:2-1-x86_64.pkg.tar.zst", *STYLE, name="style", version="1:2-1")
    assert not aur_root.PACKAGE.fullmatch(epoch.name), "the shim cannot stage a file name with an epoch colon"
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    seen = []
    sh.interactive = lambda argv: seen.append(sorted(p.name for p in (hand / "stage").iterdir())) or 0
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(epoch), str(good)], 7, sh) == 0
    assert seen == [[good.name]]
    held = json.loads(learned.read_text())["style"]
    assert held["version"] == "1:2-1" and "file name" in held["why"]


def test_no_more_packages_than_the_shim_takes_are_staged(hand, monkeypatch) -> None:
    monkeypatch.setattr(aur_root, "MAX_FILES", 1)
    monkeypatch.setattr(aur_root, "installed_foreign", lambda: {**INSTALLED, "style": {**INSTALLED["app-bin"], "paths": []}})
    first = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    second = package(hand / "style-2-1-x86_64.pkg.tar.zst", *STYLE, name="style")
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    seen = []
    sh.interactive = lambda argv: seen.append(sorted(p.name for p in (hand / "stage").iterdir())) or 0
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(first), str(second)], 7, sh) == 0
    assert seen == [[first.name]]


@pytest.mark.parametrize("content", ["[]", "null", "5", '"x"', "[" * 200000, "{"])
def test_a_bad_aur_by_hand_file_does_not_stop_the_review_or_the_hand_over(tmp_path, hand, monkeypatch, content) -> None:
    (tmp_path / stores.BY_HAND).write_text(content)
    assert stores.aur_review(Look({"x": "1"}, [entry("x")]), 7, tmp_path / "maintainers.json")[1] == []
    monkeypatch.setattr(aur_handoff, "LEARNED", tmp_path / stores.BY_HAND)
    aur_handoff.remember({"x": {"why": "w"}})
    assert json.loads((tmp_path / stores.BY_HAND).read_text()) == {"x": {"why": "w"}}


def test_a_list_that_cannot_be_written_does_not_stop_the_hand_over(hand, monkeypatch) -> None:
    blocked = hand / "aur-by-hand.json"
    blocked.mkdir()
    monkeypatch.setattr(aur_handoff, "LEARNED", blocked)
    monkeypatch.setattr(aur_root, "installed_foreign", lambda: {**INSTALLED, "style": {**INSTALLED["app-bin"], "paths": []}})
    good = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    plugin = package(hand / "style-2-1-x86_64.pkg.tar.zst", ("usr/lib/qt6/plugins/style.so", "file", {}), name="style")
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    sh.interactive = lambda argv: 0
    assert aur_handoff.handoff([*aur_handoff.INSTALL, str(plugin), str(good)], 7, sh) == 0


def test_the_reason_of_a_set_aside_entry_reaches_the_screen_without_control_characters(tmp_path) -> None:
    (tmp_path / stores.BY_HAND).write_text(json.dumps({"x": {"why": "bad\x1b[31m\nred"}, "y": {"why": ""}, "z": ""}))
    held = stores.aur_review(Look({"x": "1", "y": "1", "z": "1"}, [entry("x"), entry("y"), entry("z")]), 7, tmp_path / "maintainers.json")[1]
    assert held == ["x: bad[31mred", "y: set aside after the shim refused it", "z: set aside after the shim refused it"]


def test_a_failed_pacman_listing_does_not_hold_a_package(tmp_path) -> None:
    class Failing(Look):
        def capture(self, argv, timeout=30):
            from arch_updater.engine import CommandResult

            if argv[:3] == ["pacman", "-Qlq", "--"]:
                return CommandResult(argv, 1, "error: package 'x' was not found\n")
            return super().capture(argv, timeout)

    assert stores.aur_review(Failing({"x": "1"}, [entry("x")]), 7, tmp_path / "maintainers.json")[1] == []


def test_the_version_is_read_from_the_package_file_name() -> None:
    read = aur_handoff.built_version
    assert read("app-bin", Path("/x/app-bin-2.1-3-x86_64.pkg.tar.zst")) == "2.1-3"
    assert read("app", Path("/x/app-1:4.0-1-any.pkg.tar.xz")) == "1:4.0-1"
    assert read("app", Path("/x/other-1-1-any.pkg.tar.zst")) is None


@pytest.mark.parametrize("argv", [["pacman", "-S", "--asdeps", "--", "x"], ["pacman", "-U", "--overwrite", "*", "--", "x"], ["pacman", "-U", "--noconfirm", "--config", "/etc/pacman.conf", "--"], ["sh", "-c", "id"]])
def test_the_hand_over_refuses_anything_but_a_plain_install(hand, argv) -> None:
    sh = Hand([])
    assert aur_handoff.handoff(argv, 7, sh) == 1 and sh.typed == []


def test_the_ignore_list_yay_passes_on_does_not_stop_the_install(hand) -> None:
    # Live 2026-10-22, 11-01, 11-08: yay forwards the run's --ignore to pacman -U and every AUR install was refused.
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    sh = Hand([{"Name": "app-bin", "LastModified": time.time() - 30 * 86400}])
    argv = ["pacman", "-U", "--ignore", "darkly-bin", "--ignore", "discord-canary", "--ignore=qtengine,powershell-bin", "--noconfirm", "--config", "/etc/pacman.conf", "--", str(built)]
    assert aur_handoff.handoff(argv, 7, sh) == 0
    assert sh.typed == [["sudo", "-n", str(aur_handoff.SHIM)]]


def test_a_held_package_that_was_built_anyway_is_not_installed(hand) -> None:
    built = package(hand / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    sh = Hand([{"Name": "app-bin", "LastModified": 0}])
    argv = ["pacman", "-U", "--ignore", "app-bin", "--noconfirm", "--config", "/etc/pacman.conf", "--", str(built)]
    assert aur_handoff.handoff(argv, 7, sh) == 1 and sh.typed == []


@pytest.mark.parametrize("argv", [
    ["pacman", "-U", "--noconfirm", "--config", "/etc/pacman.conf", "--ignore", "--", "x"],
    ["pacman", "-U", "--ignore", "a", "--overwrite", "*", "--noconfirm", "--config", "/etc/pacman.conf", "--", "x"],
    ["pacman", "-U", "--ignore", "a", "--noconfirm", "--config", "/etc/pacman.conf", "--"],
])
def test_setting_the_ignore_list_aside_lets_nothing_else_through(hand, argv) -> None:
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
    def __init__(self, installed, results, paths=None):
        self.installed, self.results, self.paths = installed, results, paths or {}

    def capture(self, argv, timeout=30):
        from arch_updater.engine import CommandResult

        if argv == ["pacman", "-Qqm"]:
            return CommandResult(argv, 0, "\n".join(self.installed) + "\n")
        if argv == ["pacman", "-Qm"]:
            return CommandResult(argv, 0, "\n".join(f"{n} {v}" for n, v in self.installed.items()) + "\n")
        if argv[0] == "vercmp":
            return CommandResult(argv, 0, "1" if argv[1] != argv[2] else "0")
        if argv[:3] == ["pacman", "-Qlq", "--"]:
            return CommandResult(argv, 0, "".join(f"{path}\n" for path in self.paths.get(argv[3], ["/opt/", f"/opt/{argv[3]}"])))
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


def test_an_update_the_shim_would_refuse_is_never_built_unasked(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    paths = {"style": ["/usr/", "/usr/lib/", "/usr/lib/qt6/", "/usr/lib/qt6/plugins/", "/usr/lib/qt6/plugins/style.so"],
             "driver": ["/usr/", "/usr/lib/", "/usr/lib/cups/", "/usr/lib/cups/filter/", "/usr/lib/cups/filter/rastertoescpx"],
             "app-bin": ["/opt/", "/opt/app/", "/opt/app/app", "/usr/", "/usr/bin/", "/usr/bin/app", "/usr/share/", "/usr/share/applications/", "/usr/share/applications/app.desktop", "/usr/lib/", "/usr/lib/app/", "/usr/lib/app/data"]}
    look = Look({name: "1" for name in paths}, [entry(name) for name in paths], paths)
    assert stores.aur_review(look, 7, state)[1] == [
        "driver: installs usr/lib/cups, where root reads; the shim refuses that",
        "style: installs usr/lib/qt6, where root reads; the shim refuses that",
    ]


def test_a_package_the_shim_refused_once_waits_for_a_person(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    (tmp_path / stores.BY_HAND).write_text(json.dumps({"x": "the shim refused it: has an install script"}))
    assert stores.aur_review(Look({"x": "1"}, [entry("x")]), 7, state)[1] == ["x: the shim refused it: has an install script"]


def test_a_refusal_holds_the_refused_version_only(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    (tmp_path / stores.BY_HAND).write_text(json.dumps({"x": {"why": "the shim refused it: has an install script", "version": "2-1"}}))
    assert stores.aur_review(Look({"x": "1"}, [entry("x", version="2-1")]), 7, state)[1] == ["x: the shim refused it: has an install script"]
    assert stores.aur_review(Look({"x": "1"}, [entry("x", version="3-1")]), 7, state)[1] == [], "a newer version is tried again"


def test_an_entry_without_a_version_holds_every_version(tmp_path) -> None:
    state = tmp_path / "maintainers.json"
    (tmp_path / stores.BY_HAND).write_text(json.dumps({"x": {"why": "old entry"}, "y": 42}))
    assert stores.aur_review(Look({"x": "1", "y": "1"}, [entry("x", version="9-1"), entry("y")]), 7, state)[1] == ["x: old entry"]


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


FONT = {"ttf-x": {"version": "1-1", "conflicts": [], "replaces": [], "provides": [], "paths": [
    "/usr/", "/usr/share/", "/usr/share/fonts/", "/usr/share/fonts/ttf-x/", "/usr/share/fonts/ttf-x/X.ttf"]}}


def test_a_shared_folder_pacman_lists_as_owned_is_not_a_folder_of_its_own(tmp_path) -> None:
    """pacman -Ql lists usr/share/fonts for any font package; fontconfig.hook runs fc-cache as root over it."""
    evil = package(tmp_path / "a.pkg.tar.zst", ("usr/share/fonts/zzz-evil/p.ttf", "file", {"data": b"x"}), name="ttf-x")
    with pytest.raises(aur_root.Refused, match="outside the allowed places"):
        aur_root.inspect(evil, FONT)
    assert aur_root.own_folders("ttf-x", FONT["ttf-x"]["paths"]) == {"ttf-x"}
