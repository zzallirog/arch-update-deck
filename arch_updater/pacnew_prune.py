#!/usr/bin/python3 -I
"""Delete every .pacnew under /etc that is byte for byte the file it sits beside. Run by sudo with no arguments.

A .pacnew is pacman's new version of a configuration file you changed. When it
is the same as the live file, nothing is left to merge and it only clutters
`pacdiff`. This removes exactly those, and nothing else:

  - only under /etc, walked without following any link;
  - only a regular file whose name ends in `.pacnew`, beside a regular file
    (not a link) of the same name without it, with the same bytes;
  - only in a folder owned by uid 0 that nobody else may write, so no other
    account can swap a file in between the comparison and the deletion.

It reads nothing from the command line or the environment. Each deletion is
printed as `removed <path>`; anything else is left as it was. Install it
owned by uid 0, mode 0755, as /usr/local/libexec/arch-update-pacnew-prune.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ETC = Path("/etc")
SUFFIX = ".pacnew"
CHUNK = 1 << 16


def _regular(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def same_bytes(first: Path, second: Path) -> bool:
    """Byte for byte, read without following a link at the last step."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        one, two = os.open(first, flags), os.open(second, flags)
    except OSError:
        return False
    try:
        if os.fstat(one).st_size != os.fstat(two).st_size:
            return False
        while True:
            left, right = os.read(one, CHUNK), os.read(two, CHUNK)
            if left != right:
                return False
            if not left:
                return True
    finally:
        os.close(one)
        os.close(two)


def safe_folder(folder: Path, owner: int) -> bool:
    try:
        info = folder.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022


def identical(pacnew: Path, owner: int = 0) -> bool:
    """A .pacnew that may go: a plain file, beside its plain live file, the same bytes, in a folder only `owner` writes."""
    if not pacnew.name.endswith(SUFFIX) or len(pacnew.name) == len(SUFFIX):
        return False
    live = pacnew.with_name(pacnew.name[: -len(SUFFIX)])
    return (
        safe_folder(pacnew.parent, owner)
        and _regular(pacnew) is not None
        and _regular(live) is not None
        and same_bytes(pacnew, live)
    )


def prune(etc: Path = ETC, owner: int = 0) -> list[str]:
    """Delete each identical .pacnew under `etc`; the paths deleted. Links to folders are not entered."""
    removed: list[str] = []
    for folder, _dirs, files in os.walk(etc, followlinks=False):
        for name in sorted(files):
            candidate = Path(folder) / name
            if identical(candidate, owner):
                try:
                    candidate.unlink()
                except OSError:
                    continue
                removed.append(str(candidate))
    return removed


def main(argv: list[str]) -> int:
    if argv[1:]:
        print("arch-update-pacnew-prune takes no arguments", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("arch-update-pacnew-prune must run as uid 0 (through sudo)", file=sys.stderr)
        return 2
    for path in prune():
        print(f"removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
