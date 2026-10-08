#!/usr/bin/python3 -I
"""Install AUR package updates that the account built, as root, without trusting the account.

sudo runs this with no arguments (the sudoers rule ends in ""), so nothing on the
command line can steer it. It takes the packages from one fixed folder of the
calling account, copies them into a folder only root can write, and checks the
copies, not the originals: what was checked is what pacman installs.

A package passes only if all of this holds:
  - it replaces a foreign package already installed, with the same version (a
    rebuild) or a newer one, never an older one;
  - it adds no conflict, replacement or provision the installed version lacks;
  - it carries no install script, no setuid or setgid file, no device, no hard
    link, no extended attribute (file capabilities live there), nothing not
    owned by root;
  - every path lies where nothing running as root reads it (see allowed());
  - no path goes through a symbolic link of the same package.
Anything else refuses the whole batch: nothing is installed, exit 2.

What it cannot check: that the package is what the PKGBUILD meant. The build
ran as the account; the cool-down before the build is the defence there.
"""

from __future__ import annotations

import os
import posixpath
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

STAGE = ".cache/arch-updater/aur-stage"  # under the calling account's home
WORK = Path("/var/cache/arch-update-aur")
PACKAGE = re.compile(r"[A-Za-z0-9@._+-]+\.pkg\.tar\.zst")
MAX_FILES = 32
MAX_PACKAGE = 2 << 30  # bytes, compressed
MAX_CONTENT = 8 << 30  # bytes, all members of one package
METADATA = {".PKGINFO", ".BUILDINFO", ".MTREE", ".CHANGELOG"}
# Anywhere below these: nothing running as root reads it.
OPEN = (
    "opt/",
    "usr/bin/",
    "usr/share/applications/",
    "usr/share/icons/",
    "usr/share/pixmaps/",
    "usr/share/doc/",
    "usr/share/licenses/",
    "usr/share/man/",
    "usr/share/locale/",
    "usr/share/metainfo/",
    "usr/share/bash-completion/completions/",
    "usr/share/zsh/site-functions/",
    "usr/share/fish/vendor_completions.d/",
    "usr/src/debug/",  # debug packages: read by gdb as the account
    "usr/lib/debug/",
)
# Where a package may have a folder of its own: usr/lib/<x>, usr/share/<x>, usr/include/<x>.
OWN = ("lib", "share", "include")
# Folders of usr/lib and usr/share that root reads or runs from, whatever the package is called.
SYSTEM = {
    "binfmt.d", "dbus-1", "depmod.d", "dkms", "environment.d", "firmware", "initcpio", "kernel", "libalpm",
    "modprobe.d", "modules", "modules-load.d", "pam.d", "polkit-1", "security", "sysctl.d", "systemd",
    "sysusers.d", "tmpfiles.d", "udev", "xorg", "mime", "X11", "kbd", "factory", "ca-certificates", "pacman",
    "gnupg", "sudo", "cron", "elogind", "NetworkManager", "bluetooth", "cups", "grub", "os-release",
}


class Refused(Exception):
    pass


def own_folders(pkgname: str, installed_paths: list[str]) -> set[str]:
    """usr/lib/<x>, usr/share/<x> and usr/include/<x> folders this package may fill: its own name, and what its installed version holds."""
    names = {pkgname, re.sub(r"-(bin|appimage)$", "", pkgname)}
    for path in installed_paths:
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "usr" and parts[1] in OWN:
            names.add(parts[2])
    return {name for name in names if name and name not in SYSTEM and not name.endswith((".so", ".conf")) and "." not in name[:1]}


def allowed(path: str, folders: set[str]) -> bool:
    """True when `path` (no leading slash) is a file or link place nothing running as root reads."""
    if path.startswith(OPEN):
        return True
    parts = path.split("/")
    return len(parts) >= 4 and parts[0] == "usr" and parts[1] in OWN and parts[2] in folders


def ancestor(path: str, folders: set[str]) -> bool:
    """True when `path` is a folder above an allowed place (usr, usr/share, opt...): a package lists those too."""
    prefix = path.rstrip("/") + "/"
    places = [*OPEN, *(f"usr/{where}/{name}/" for where in OWN for name in folders)]
    return any(place.startswith(prefix) for place in places)


def landed(folder: str, folders: set[str]) -> bool:
    """True when the folder, followed through whatever links are on disk now, is still an allowed place or above one."""
    real = os.path.realpath("/" + folder).lstrip("/")
    return real == folder or allowed(real + "/", folders) or ancestor(real, folders)


def parse_pkginfo(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("#") or " = " not in line:
            continue
        key, _, value = line.partition(" = ")
        fields.setdefault(key.strip(), []).append(value.strip())
    return fields


def inspect(package: Path, installed: dict[str, dict]) -> dict:
    """Read one package copy and return its name and version, or raise Refused with every reason found.

    `installed` maps each installed foreign package to {"version", "conflicts", "replaces", "provides", "paths"}.
    """
    problems: list[str] = []
    members: list[tarfile.TarInfo] = []
    info: dict[str, list[str]] = {}
    total = 0
    try:
        with tarfile.open(package, "r|zst") as archive:
            for member in archive:
                members.append(member)
                total += max(member.size, 0)
                if total > MAX_CONTENT or len(members) > 500_000:
                    raise Refused(f"{package.name}: too large to check")
                if member.name == ".PKGINFO" and member.isfile():
                    handle = archive.extractfile(member)
                    info = parse_pkginfo(handle.read(1 << 20).decode("utf-8", "replace")) if handle else {}
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise Refused(f"{package.name}: not a readable package ({type(exc).__name__}: {exc})") from None
    name = (info.get("pkgname") or [""])[0]
    version = (info.get("pkgver") or [""])[0]
    if not name or not version:
        raise Refused(f"{package.name}: no .PKGINFO with pkgname and pkgver")
    old = installed.get(name)
    if old is None:
        raise Refused(f"{name}: not an installed foreign package; only updates are installed this way")
    if vercmp(version, old["version"]) < 0:
        problems.append(f"{name}: {version} is older than the installed {old['version']}")
    for field, key in (("conflict", "conflicts"), ("replaces", "replaces"), ("provides", "provides")):
        new = set(info.get(field, [])) - set(old[key])
        if new:
            problems.append(f"{name}: adds {field} {', '.join(sorted(new))}")
    folders = own_folders(name, old["paths"])
    links = {m.name.rstrip("/") for m in members if m.issym()}
    for member in members:
        path = member.name
        clean = posixpath.normpath(path) if path else ""
        if not path or path.startswith("/") or clean != path.rstrip("/") or clean.startswith("..") or "/../" in f"/{path}/":
            problems.append(f"{name}: bad path {path!r}")
            continue
        if "/" not in clean and clean.startswith("."):
            if clean == ".INSTALL":
                problems.append(f"{name}: has an install script (.INSTALL), which runs as root")
            elif clean not in METADATA:
                problems.append(f"{name}: unknown metadata file {clean}")
            continue
        if member.pax_headers and any(key.startswith(("SCHILY.xattr", "LIBARCHIVE.xattr", "SCHILY.acl", "SCHILY.fflags")) for key in member.pax_headers):
            problems.append(f"{name}: {clean} carries extended attributes or ACLs")
        if member.uid != 0 or member.gid != 0:
            problems.append(f"{name}: {clean} is not owned by root ({member.uid}:{member.gid})")
        if member.mode & (stat.S_ISUID | stat.S_ISGID):
            problems.append(f"{name}: {clean} is setuid or setgid")
        if member.mode & stat.S_IWOTH and not member.issym():
            problems.append(f"{name}: {clean} is writable by anyone")
        parents = clean.split("/")
        if any("/".join(parents[:depth]) in links for depth in range(1, len(parents))):
            problems.append(f"{name}: {clean} goes through a link of the same package")
        elif len(parents) > 1 and not landed(posixpath.dirname(clean), folders):
            problems.append(f"{name}: {clean} goes through a link already on disk")
        if member.isdir():
            if not (allowed(clean + "/", folders) or ancestor(clean, folders)):
                problems.append(f"{name}: folder {clean} is outside the allowed places")
            elif member.mode & stat.S_ISVTX:
                problems.append(f"{name}: folder {clean} is sticky")
            continue
        if member.islnk() or not (member.isfile() or member.issym()):
            problems.append(f"{name}: {clean} is a hard link or a special file")
            continue
        if not allowed(clean, folders):
            problems.append(f"{name}: {clean} is outside the allowed places")
            continue
        if member.issym():
            target = member.linkname
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(clean), target))
            # A system file (a bundled app pointing at /usr/lib/libssl.so.3) is fine; a system folder is a way in.
            system_file = resolved.startswith(("usr/lib/", "usr/share/")) and os.path.isfile("/" + resolved)
            if resolved.startswith("..") or not (allowed(resolved, folders) or system_file):
                problems.append(f"{name}: link {clean} points to {target}")
    if problems:
        raise Refused("\n".join(problems))
    return {"name": name, "version": version}


def vercmp(a: str, b: str) -> int:
    result = subprocess.run(["/usr/bin/vercmp", a, b], capture_output=True, text=True, check=True)
    return int(result.stdout.strip())


def installed_foreign() -> dict[str, dict]:
    """Every installed foreign package with what the checks compare against."""
    names = subprocess.run(["/usr/bin/pacman", "-Qqm"], capture_output=True, text=True, check=False).stdout.split()
    if not names:
        return {}
    table: dict[str, dict] = {name: {"version": "", "conflicts": [], "replaces": [], "provides": [], "paths": []} for name in names}
    listing = subprocess.run(["/usr/bin/pacman", "-Qi", "--", *names], capture_output=True, text=True, check=True).stdout
    keys = {"Version": "version", "Conflicts With": "conflicts", "Replaces": "replaces", "Provides": "provides"}
    current = None
    for line in listing.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "Name":
            current = table.get(value)
        elif current is not None and key in keys:
            current[keys[key]] = value if key == "Version" else ([] if value == "None" else value.split())
    for line in subprocess.run(["/usr/bin/pacman", "-Ql", "--", *names], capture_output=True, text=True, check=True).stdout.splitlines():
        name, _, path = line.partition(" ")
        if name in table:
            table[name]["paths"].append(path)
    return table


def take(stage_fd: int, entry: str, into: Path) -> Path:
    """Copy one staged file into root's folder without following a link and without reading it twice."""
    fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=stage_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PACKAGE:
            raise Refused(f"{entry}: not a regular file of a sane size")
        target = into / entry
        with os.fdopen(os.dup(fd), "rb") as source, open(target, "xb") as copy:
            shutil.copyfileobj(source, copy, 1 << 20)
        return target
    finally:
        os.close(fd)


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) != 1:
        print("run by sudo, with no arguments", file=sys.stderr)
        return 64
    uid = int(os.environ.get("SUDO_UID", "-1"))
    if uid <= 0:
        print("SUDO_UID is missing: run through sudo from the account that built the packages", file=sys.stderr)
        return 64
    home = Path(pwd.getpwuid(uid).pw_dir)
    os.umask(0o077)
    WORK.mkdir(mode=0o700, exist_ok=True)
    os.chmod(WORK, 0o700)
    work = Path(tempfile.mkdtemp(dir=WORK))
    try:
        try:
            stage_fd = os.open(home / STAGE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise Refused(f"no stage folder {home / STAGE}: {exc.strerror}") from None
        try:
            if os.fstat(stage_fd).st_uid != uid:
                raise Refused(f"{home / STAGE} is not owned by the calling account")
            entries = sorted(os.listdir(stage_fd))
            if not entries:
                print("nothing staged")
                return 0
            if len(entries) > MAX_FILES or any(not PACKAGE.fullmatch(entry) for entry in entries):
                raise Refused(f"the stage holds something that is not a package file: {', '.join(entries[:5])}")
            copies = [take(stage_fd, entry, work) for entry in entries]
        finally:
            os.close(stage_fd)
        installed = installed_foreign()
        names = []
        for copy in copies:
            checked = inspect(copy, installed)
            names.append(f"{checked['name']} {checked['version']}")
        print("checked: " + "; ".join(names), flush=True)
        env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
        result = subprocess.run(["/usr/bin/pacman", "-U", "--noconfirm", "--config", "/etc/pacman.conf", "--", *map(str, copies)], env=env, cwd="/", check=False)
        return 0 if result.returncode == 0 else 1
    except Refused as refusal:
        print(f"refused, nothing installed:\n{refusal}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
