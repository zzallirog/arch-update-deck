"""Room for the update on each filesystem it writes to, against a scripted set of mounts and a scripted pacman."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arch_updater import space, watch
from arch_updater.shell import Shell

GiB = 1024**3
MiB = 1024**2
LOW_ON_SYSTEM = space.LOW

SI = """Repository      : core
Name            : linux
Version         : 6.18.1-1
Download Size   : 140.00 MiB
Installed Size  : 150.00 MiB

Repository      : core
Name            : glibc
Download Size   : 10.00 MiB
Installed Size  : 50.00 MiB

Repository      : other
Name            : glibc
Download Size   : 99.00 GiB
Installed Size  : 99.00 GiB
"""
QI = """Name            : linux
Installed Size  : 148.00 MiB

Name            : glibc
Installed Size  : 49.00 MiB
"""


def ok(output=""):
    return SimpleNamespace(returncode=0, output=output)


class Disks(Shell):
    """Mounts by path prefix; the longest prefix a path starts with is its filesystem."""

    def __init__(self, mounts, queue=(2, ""), si=SI, qi=QI, tools=("checkupdates",)):
        self.mounts = mounts  # prefix -> {"free": bytes, "ro": bool}
        self.queue, self.si, self.qi, self.tools = queue, si, qi, set(tools)
        self.calls = []

    def _mount(self, path):
        return max((prefix for prefix in self.mounts if path == prefix or path.startswith(prefix.rstrip("/") + "/")), key=len)

    def device(self, path):
        return hash(self._mount(path))

    def free_bytes(self, path):
        return self.mounts[self._mount(path)]["free"]

    def total_bytes(self, path):
        return self.mounts[self._mount(path)].get("total", 100 * GiB)

    def read_only(self, path):
        return self.mounts[self._mount(path)].get("ro", False)

    def has(self, tool):
        return tool in self.tools

    def capture(self, argv, timeout=30):
        self.calls.append(argv)
        if argv[0] == "pacman-conf":
            return ok({"CacheDir": "/var/cache/pacman/pkg/\n", "DBPath": "/var/lib/pacman/\n"}[argv[1]])
        if "checkupdates" in argv:
            code, text = self.queue
            return SimpleNamespace(returncode=code, output=text)
        if "-Si" in argv:
            return ok(self.si)
        if "-Qi" in argv:
            return ok(self.qi)
        raise AssertionError(argv)


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    """Paths are taken as they are, and the kernel's boot files live in a temporary /boot."""
    monkeypatch.setattr(space, "_existing", lambda path: path)
    presets, boot = tmp_path / "mkinitcpio.d", tmp_path / "boot"
    presets.mkdir()
    boot.mkdir()
    (presets / "linux.preset").write_text("")
    (boot / "vmlinuz-linux").write_bytes(b"x" * (15 * MiB))
    (boot / "initramfs-linux.img").write_bytes(b"x" * (40 * MiB))
    monkeypatch.setattr(space, "PRESETS", presets)
    monkeypatch.setattr(space, "BOOT", boot)


KERNEL_QUEUE = (0, "linux 6.17.9-1 -> 6.18.1-1\nglibc 2.42-1 -> 2.42-2\n")
GLIBC_QUEUE = (0, "glibc 2.42-1 -> 2.42-2\n")


def test_sizes_are_read_as_pacman_prints_them() -> None:
    assert [space.size(text) for text in ("512 B", "1.50 KiB", "2.00 MiB", "1.25 GiB")] == [512, 1536, 2 * MiB, int(1.25 * GiB)]
    fields = space._fields(SI)
    assert fields["glibc"] == {"Download Size": 10 * MiB, "Installed Size": 50 * MiB}, "the first repository wins, as pacman takes it"


def test_the_plan_counts_download_growth_and_a_kernels_boot_files() -> None:
    plan = space.plan(Disks({"/": {"free": 100 * GiB}}, KERNEL_QUEUE), db=None)
    assert plan.names == ["linux", "glibc"] and plan.read
    assert plan.download == 150 * MiB
    assert plan.growth == (150 + 50 - 148 - 49) * MiB
    assert plan.boot == 55 * MiB, "the kernel's image and initramfs must fit once more"


@pytest.mark.parametrize(
    ("queue", "tools", "read", "note"),
    [
        ((2, ""), ("checkupdates",), True, ""),  # 2: nothing waits
        ((1, "==> ERROR"), ("checkupdates",), False, "checkupdates exited 1"),
        ((2, ""), (), False, "checkupdates is not installed"),
    ],
)
def test_a_queue_that_cannot_be_read_is_said_so_never_taken_for_empty(queue, tools, read, note) -> None:
    plan = space.plan(Disks({"/": {"free": 100 * GiB}}, queue, tools=tools))
    assert (plan.read, plan.note, plan.download, plan.growth) == (read, note, 0, 0)


def test_a_size_pacman_did_not_tell_is_not_guessed() -> None:
    plan = space.plan(Disks({"/": {"free": 100 * GiB}}, KERNEL_QUEUE, si=SI.split("\n\n")[0]))
    assert not plan.read and "glibc" in plan.note


def test_the_queue_is_read_into_a_database_of_its_own_in_c_locale() -> None:
    sh = Disks({"/": {"free": 100 * GiB}}, GLIBC_QUEUE)
    space.plan(sh)
    first = sh.calls[0]
    assert first[:3] == ["env", f"CHECKUPDATES_DB={space.database()}", "LC_ALL=C"] and "checkupdates" in first
    assert "/var/lib/pacman" not in str(space.database()) and "/tmp/checkup-db" not in str(space.database())
    assert all(call[:2] == ["env", "LC_ALL=C"] for call in sh.calls[1:] if "pacman" in call)


# ── per filesystem ─────────────────────────────────────────────────────────────


def test_one_filesystem_with_room_passes() -> None:
    sh = Disks({"/": {"free": 50 * GiB}}, KERNEL_QUEUE)
    assert space.shortfalls(sh, space.plan(sh)) == []


def test_the_floor_on_slash_stays_six_gib_whatever_the_update_brings() -> None:
    sh = Disks({"/": {"free": int(5.9 * GiB)}})
    assert space.shortfalls(sh, space.plan(sh)) == [(LOW_ON_SYSTEM, "5.9 GiB free on /")]


def test_a_separate_boot_too_small_for_a_new_kernel_fails_on_its_own_case() -> None:
    sh = Disks({"/": {"free": 50 * GiB}, "/boot": {"free": 300 * MiB}}, KERNEL_QUEUE)
    (found,) = space.shortfalls(sh, space.plan(sh))
    assert found[0] == "MOUNT-SPACE-LOW", "trimming the package cache would not free /boot"
    assert found[1].startswith("300 MiB free on /boot, this update needs 567 MiB (55 MiB of kernel boot files, 512 MiB spare)")


def test_a_small_filesystem_keeps_a_share_of_itself_spare_not_half_a_gib() -> None:
    """A 1 GiB /boot with 0.5 GiB free took a kernel update fine; 0.5 GiB spare on top would have refused it."""
    sh = Disks({"/": {"free": 50 * GiB}, "/boot": {"free": 497 * MiB, "total": 1022 * MiB}}, KERNEL_QUEUE)
    assert space.shortfalls(sh, space.plan(sh)) == []
    sh.mounts["/boot"]["free"] = 100 * MiB
    (found,) = space.shortfalls(sh, space.plan(sh))
    assert found == ("MOUNT-SPACE-LOW", "100 MiB free on /boot, this update needs 106 MiB (55 MiB of kernel boot files, 51 MiB spare)")


def test_a_small_boot_is_not_looked_at_when_no_kernel_is_coming() -> None:
    sh = Disks({"/": {"free": 50 * GiB}, "/boot": {"free": 1 * MiB, "ro": True}}, GLIBC_QUEUE)
    assert space.shortfalls(sh, space.plan(sh)) == []


def test_a_separate_cache_is_checked_for_the_download_and_trimming_it_is_the_repair() -> None:
    sh = Disks({"/": {"free": 50 * GiB}, "/var": {"free": 300 * MiB}}, KERNEL_QUEUE)
    (found,) = space.shortfalls(sh, space.plan(sh))
    assert found[0] == LOW_ON_SYSTEM and "free on /var/lib/pacman" in found[1] and "150 MiB to download" in found[1]


def test_slash_fails_above_the_floor_when_the_update_needs_more() -> None:
    big = SI.replace("150.00 MiB", "9.00 GiB")
    sh = Disks({"/": {"free": 8 * GiB}}, KERNEL_QUEUE, si=big)
    (found,) = space.shortfalls(sh, space.plan(sh))
    assert found[0] == LOW_ON_SYSTEM and found[1].startswith("8.0 GiB free on /, this update needs 9.6 GiB (150 MiB to download, +8.9 GiB installed, 55 MiB of kernel boot files, 512 MiB spare)")


def test_what_the_update_frees_is_credited() -> None:
    shrinking = SI.replace("Installed Size  : 150.00 MiB", "Installed Size  : 10.00 MiB")
    sh = Disks({"/": {"free": 50 * GiB}, "/usr": {"free": 100 * MiB}}, KERNEL_QUEUE, si=shrinking)
    plan = space.plan(sh)
    assert plan.growth < 0
    assert space.shortfalls(sh, plan) == [], "a separate /usr that only shrinks is not short of room"
    growing = Disks({"/": {"free": 50 * GiB}, "/usr": {"free": 100 * MiB}}, KERNEL_QUEUE)
    assert [case for case, _ in space.shortfalls(growing, space.plan(growing))] == ["MOUNT-SPACE-LOW"]


@pytest.mark.parametrize(("mounts", "label"), [({"/": {"free": 50 * GiB, "ro": True}}, "/"), ({"/": {"free": 50 * GiB}, "/var": {"free": 50 * GiB, "ro": True}}, "/var/lib/pacman")])
def test_a_read_only_filesystem_the_update_writes_to_is_its_own_refusal(mounts, label) -> None:
    sh = Disks(mounts)
    assert space.shortfalls(sh, space.plan(sh)) == [("READ-ONLY-FILESYSTEM", f"{label} is mounted read-only; nothing can be updated on it")]


def test_a_read_only_boot_refuses_a_kernel_update() -> None:
    sh = Disks({"/": {"free": 50 * GiB}, "/boot": {"free": 5 * GiB, "ro": True}}, KERNEL_QUEUE)
    assert [case for case, _ in space.shortfalls(sh, space.plan(sh))] == ["READ-ONLY-FILESYSTEM"]


def test_the_probe_gives_one_verdict_per_filesystem_that_falls_short() -> None:
    sh = Disks({"/": {"free": 1 * GiB}, "/boot": {"free": 1 * MiB}}, KERNEL_QUEUE)
    verdicts = watch.probe_disk(watch.Scene(sh, {}, None, None, None)).verdicts
    assert [(verdict.probe, verdict.status, verdict.case) for verdict in verdicts] == [("disk", "fail", LOW_ON_SYSTEM), ("disk", "fail", "MOUNT-SPACE-LOW")]


def test_the_probe_says_when_only_the_floor_was_checked() -> None:
    (verdict,) = watch.probe_disk(watch.Scene(Disks({"/": {"free": 50 * GiB}}, tools=()), {}, None, None, None)).verdicts
    assert verdict.status == "pass" and "only the floor on / was checked" in verdict.detail
