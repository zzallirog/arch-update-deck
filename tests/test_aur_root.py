"""The root shim: what it lets through and what it refuses, on package files built here."""

from __future__ import annotations

import io
import json
import os
import subprocess
import types
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
        with pytest.raises(aur_root.Refused):
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


def test_only_a_dependency_no_repository_has_holds_an_update(tmp_path) -> None:
    needy = {**entry("zap"), "MakeDepends": ["git", "python-build", "libaur-only>=2"], "Depends": ["qt6-base"]}
    repo_only = {**entry("fine"), "MakeDepends": ["python-build"]}
    plain = {**entry("ok"), "Depends": ["qt6-base"]}
    look = Look({"zap": "1", "fine": "1", "ok": "1"}, [needy, repo_only, plain], absent=["python-build", "libaur-only"], aur_only=["libaur-only"])
    held = stores.aur_review(look, 7, tmp_path / "maintainers.json")[1]
    assert held == ["zap: needs libaur-only>=2 installed first (the root shim takes only plain repository names)"], "python-build comes from a repository: the shim installs it"


def test_a_dependency_the_shim_cannot_take_holds_an_update_before_the_build(tmp_path) -> None:
    class Offered(Look):
        def capture(self, argv, timeout=30):
            from arch_updater.engine import CommandResult

            if argv[:2] == ["pacman", "-Sp"]:  # a repository offers it through a range or a provider, yet the shim takes plain names only
                return CommandResult(argv, 0, "")
            return super().capture(argv, timeout)

    needy = {**entry("zap"), "Depends": ["ranged>=2", "java-runtime"]}
    look = Offered({"zap": "1"}, [needy], absent=["ranged", "java-runtime"], aur_only=["java-runtime"])
    held = stores.aur_review(look, 7, tmp_path / "maintainers.json")[1]
    assert held == ["zap: needs ranged>=2, java-runtime installed first (the root shim takes only plain repository names)"]


def test_a_dependency_the_shim_refuses_for_a_replacement_or_an_ignore_list_holds_an_update(tmp_path) -> None:
    needy = {**entry("zap"), "Depends": ["swaps", "pinned", "grouped", "plain"]}
    look = Look({"zap": "1"}, [needy], absent=["swaps", "pinned", "grouped", "plain"], replacing=["swaps"], groups={"grouped": "held-group other"}, ignore_pkg=["pinned"], ignore_group=["held-group"])
    held = stores.aur_review(look, 7, tmp_path / "maintainers.json")[1]
    assert held == ["zap: needs swaps, pinned, grouped installed first (the root shim takes only plain repository names)"], "plain is taken by the shim"


def test_pacman_not_answering_about_dependencies_holds_nothing(tmp_path) -> None:
    class Odd(Look):
        def capture(self, argv, timeout=30):
            from arch_updater.engine import CommandResult

            if argv[:2] == ["pacman", "-T"]:
                return CommandResult(argv, 1, "error: something else\n")
            return super().capture(argv, timeout)

    item = {**entry("zap"), "MakeDepends": ["python-build"], "Depends": None, "CheckDepends": None}
    assert stores.aur_review(Odd({"zap": "1"}, [item]), 7, tmp_path / "maintainers.json")[1] == []


def test_a_failed_pacman_listing_does_not_hold_a_package(tmp_path) -> None:
    class Failing(Look):
        def capture(self, argv, timeout=30):
            from arch_updater.engine import CommandResult

            if argv[:3] == ["pacman", "-Qlq", "--"]:
                return CommandResult(argv, 1, "error: package 'x' was not found\n")
            return super().capture(argv, timeout)

    assert stores.aur_review(Failing({"x": "1"}, [entry("x")]), 7, tmp_path / "maintainers.json")[1] == []


SYNC_INFO = """Repository      : extra
Name            : python-build
Replaces        : None

Repository      : core
Name            : git
Replaces        : None
"""


REAL_RUN = subprocess.run


class Pacman:
    """subprocess.run as the shim sees it: -Si answers from `info`, installs return `rc`; every pacman command line is kept."""

    def __init__(self, info=SYNC_INFO, rc=0, info_rc=0):
        self.info, self.rc, self.info_rc, self.calls = info, rc, info_rc, []
        self.installed, self.pinned, self.pinned_groups, self.conf_rc = [], [], [], 0

    def __call__(self, argv, **kw):
        if argv[0] == "/usr/bin/pacman-conf":
            words = self.pinned if argv[-1] == "IgnorePkg" else self.pinned_groups
            return subprocess.CompletedProcess(argv, self.conf_rc, stdout="".join(f"{w}\n" for w in words), stderr="error: config unreadable\n")
        if argv[0] != "/usr/bin/pacman":
            return REAL_RUN(argv, **kw)
        self.calls.append(list(argv))
        if argv[1] == "-Qq":
            return subprocess.CompletedProcess(argv, 0, stdout="".join(f"{w}\n" for w in self.installed), stderr="")
        if argv[1] == "-Si":
            return subprocess.CompletedProcess(argv, self.info_rc, stdout=self.info, stderr="error: package 'nope' was not found\n")
        return subprocess.CompletedProcess(argv, self.rc)


@pytest.fixture
def root_shim(tmp_path, monkeypatch):
    """The shim's main() with a fake root, a fake home and a fake pacman."""
    home = tmp_path / "home"
    stage = home / aur_root.STAGE
    stage.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(aur_root, "WORK", tmp_path / "work")
    monkeypatch.setattr(aur_root.sys, "argv", ["shim"])
    monkeypatch.setattr(aur_root.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setattr(aur_root.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(aur_root, "installed_foreign", lambda: INSTALLED)
    pacman = Pacman()
    monkeypatch.setattr(aur_root.subprocess, "run", pacman)
    pacman.stage = stage
    return pacman


def test_the_shim_installs_the_repository_packages_of_a_build_as_dependencies(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("extra/python-build\ngit\n")
    assert aur_root.main() == 0
    assert root_shim.calls == [["/usr/bin/pacman", "-Qq"], ["/usr/bin/pacman", "-Si", "--config", "/etc/pacman.conf", "--", "extra/python-build", "git"],
                               ["/usr/bin/pacman", "-S", "--asdeps", "--needed", "--noconfirm", "--config", "/etc/pacman.conf", "--", "extra/python-build", "git"]]


def test_the_shim_installs_the_dependencies_before_the_packages(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("git\n")
    package(root_shim.stage / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    assert aur_root.main() == 0
    assert [call[1] for call in root_shim.calls] == ["-Qq", "-Si", "-S", "-U"]


def test_a_failed_dependency_install_stops_before_the_packages(root_shim) -> None:
    root_shim.rc = 1
    (root_shim.stage / "repo-deps").write_text("git\n")
    package(root_shim.stage / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    assert aur_root.main() == 1
    assert [call[1] for call in root_shim.calls] == ["-Qq", "-Si", "-S"]


def test_a_dependency_already_installed_is_left_alone_even_when_no_sync_database_holds_it(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("git\n")
    root_shim.installed = ["git"]
    root_shim.info_rc = 1  # no sync database holds that name under it
    package(root_shim.stage / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    assert aur_root.main() == 0
    assert not [call for call in root_shim.calls if call[1] == "-S"], "an installed dependency is never asked for again"


def test_a_staged_link_is_refused_with_a_message_not_a_traceback(root_shim, tmp_path) -> None:
    target = package(tmp_path / "real-2-1-x86_64.pkg.tar.zst", *GOOD)
    (root_shim.stage / "app-bin-2-1-x86_64.pkg.tar.zst").symlink_to(target)
    assert aur_root.main() == 2
    assert root_shim.calls == [], "nothing was asked of pacman"


@pytest.mark.parametrize("text", ["--needed\n", "git extra\n", "git;id\n", "../git\n", "extra/Git\n", "\n", "x/y/z\n", "git>=2\n", "-S\n"])
def test_a_line_that_is_not_a_repository_name_refuses_the_whole_stage(root_shim, text) -> None:
    (root_shim.stage / "repo-deps").write_text(text)
    package(root_shim.stage / "app-bin-2-1-x86_64.pkg.tar.zst", *GOOD)
    assert aur_root.main() == 2
    assert root_shim.calls == [], "nothing was asked of pacman"


def test_the_dependency_file_must_be_a_plain_file_that_is_small_and_text(root_shim) -> None:
    good = root_shim.stage.parent / "valid-names"
    good.write_text("git\n")                                   # a link to a perfectly valid file is still refused
    for make in (lambda p: p.symlink_to(good), lambda p: p.write_text("git\n" + " " * 9000), lambda p: p.write_bytes(b"\xff\xfe"),
                 lambda p: p.write_text("git\n" * 65), lambda p: p.mkdir()):
        for old in root_shim.stage.iterdir():
            old.rmdir() if old.is_dir() and not old.is_symlink() else old.unlink()
        make(root_shim.stage / "repo-deps")
        assert aur_root.main() == 2
    assert root_shim.calls == []


def test_a_name_the_databases_do_not_know_or_that_replaces_something_refuses(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("git\n")
    root_shim.info_rc = 1
    assert aur_root.main() == 2
    root_shim.info_rc = 0
    root_shim.info = "Repository      : extra\nName            : git\nReplaces        : old-git\n"
    assert aur_root.main() == 2
    root_shim.info = SYNC_INFO
    (root_shim.stage / "repo-deps").write_text("core/python-build\n")
    assert aur_root.main() == 2, "python-build is in extra, not in core"
    assert not [c for c in root_shim.calls if c[1] == "-S"]


def test_a_package_pacman_conf_holds_back_is_refused(root_shim) -> None:
    root_shim.info = "Repository      : extra\nName            : hyprland\nGroups          : None\nReplaces        : None\n"
    (root_shim.stage / "repo-deps").write_text("hyprland\n")
    root_shim.pinned = ["hunspell-uk", "hyprland"]
    assert aur_root.main() == 2
    root_shim.pinned = []
    root_shim.info = "Repository      : extra\nName            : hyprland\nGroups          : wm-stuff other\nReplaces        : None\n"
    root_shim.pinned_groups = ["wm-stuff"]
    assert aur_root.main() == 2
    root_shim.pinned_groups = []
    assert aur_root.main() == 0
    assert [c[1] for c in root_shim.calls].count("-S") == 1, "only the unpinned run installed"


def test_a_pacman_conf_that_cannot_be_read_refuses(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("git\n")
    root_shim.conf_rc = 1
    assert aur_root.main() == 2
    assert not [c for c in root_shim.calls if c[1] == "-S"]


def test_an_installed_package_is_left_alone_not_demoted_to_a_dependency(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("git\nextra/python-build\n")
    root_shim.installed = ["git"]
    assert aur_root.main() == 0
    assert root_shim.calls[-1][-1:] == ["extra/python-build"] and "git" not in root_shim.calls[-1], root_shim.calls[-1]
    root_shim.calls.clear()
    root_shim.installed = ["git", "python-build"]
    assert aur_root.main() == 0
    assert not [c for c in root_shim.calls if c[1] == "-S"], "everything is there: nothing to install"


def test_the_stage_still_refuses_anything_else(root_shim) -> None:
    (root_shim.stage / "repo-deps").write_text("git\n")
    (root_shim.stage / "notes.txt").write_text("x")
    assert aur_root.main() == 2
    assert root_shim.calls == []


@pytest.mark.parametrize("argv,names", [
    (["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--", "extra/python-build", "git"], ["extra/python-build", "git"]),
    (["pacman", "-S", "--asdeps", "--needed", "--noconfirm", "--config", "/etc/pacman.conf", "--", "git"], ["git"]),
    (["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--"], None),
    (["pacman", "-S", "--noconfirm", "--", "git"], None),
    (["pacman", "-S", "--noconfirm", "--config", "/tmp/evil.conf", "--", "git"], None),
    (["pacman", "-S", "--overwrite", "*", "--noconfirm", "--config", "/etc/pacman.conf", "--", "git"], None),
    (["pacman", "-Sy", "--noconfirm", "--config", "/etc/pacman.conf", "--", "git"], None),
    (["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--", "git", "--overwrite=*"], None),
    (["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--", "https://evil/x.pkg.tar.zst"], None),
    (["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--"] + ["a"] * 65, None),
])
def test_only_the_plain_dependency_install_of_yay_is_taken(argv, names) -> None:
    assert aur_handoff.repo_request(argv) == names


def test_the_hand_over_stages_the_repository_names_and_runs_only_the_shim(hand) -> None:
    sh = Hand([])
    seen = []
    sh.interactive = lambda argv: seen.append((argv, {p.name: p.read_text() for p in (hand / "stage").iterdir()})) or 0
    argv = ["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--", "extra/python-installer", "extra/python-build"]
    assert aur_handoff.handoff(argv, 7, sh) == 0
    assert seen == [(["sudo", "-n", str(aur_handoff.SHIM)], {"repo-deps": "extra/python-installer\nextra/python-build\n"})]
    assert not list((hand / "stage").iterdir()), "the stage is emptied after the shim"


def test_an_older_shim_gets_no_dependency_request(hand) -> None:
    aur_handoff.SHIM.write_bytes(b"old")
    sh = Hand([])
    sh.interactive = lambda argv: pytest.fail("the shim must not run")
    argv = ["pacman", "-S", "--noconfirm", "--config", "/etc/pacman.conf", "--", "git"]
    assert aur_handoff.handoff(argv, 7, sh) == 1


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
    def __init__(self, installed, results, paths=None, absent=(), aur_only=(), replacing=(), groups=None, ignore_pkg=(), ignore_group=()):
        self.installed, self.results, self.paths, self.absent, self.aur_only = installed, results, paths or {}, set(absent), set(aur_only)
        self.replacing, self.groups, self.ignore = set(replacing), groups or {}, {"IgnorePkg": list(ignore_pkg), "IgnoreGroup": list(ignore_group)}

    def capture(self, argv, timeout=30):
        from arch_updater.engine import CommandResult

        if argv == ["pacman", "-Qqm"]:
            return CommandResult(argv, 0, "\n".join(self.installed) + "\n")
        if argv == ["pacman", "-Qm"]:
            return CommandResult(argv, 0, "\n".join(f"{n} {v}" for n, v in self.installed.items()) + "\n")
        if argv[0] == "vercmp":
            return CommandResult(argv, 0, "1" if argv[1] != argv[2] else "0")
        if argv[:2] == ["pacman", "-Si"]:
            name = argv[-1]
            info = f"Repository      : extra\nName            : {name}\nGroups          : {self.groups.get(name, 'None')}\nReplaces        : {'oldfoo' if name in self.replacing else 'None'}\n"
            return CommandResult(argv, 1 if name in self.aur_only else 0, "" if name in self.aur_only else info)
        if argv[0] == "pacman-conf" and argv[1] in self.ignore:
            return CommandResult(argv, 0, "".join(f"{word}\n" for word in self.ignore[argv[1]]))
        if argv[:3] == ["pacman", "-T", "--"]:
            missing = [dep for dep in argv[3:] if dep.split(">")[0].split("<")[0].split("=")[0] in self.absent]
            return CommandResult(argv, 127 if missing else 0, "".join(f"{dep}\n" for dep in missing))
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
