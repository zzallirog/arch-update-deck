"""Signals sent from outside to a real `arch-update auto` process, with a real tree of processes under it.

The other signal tests run the update inside pytest's own process and patch the command runner. Here
`arch-update auto` is its own process (started the way the timer starts it, in a session of its own),
`sudo` is a script that runs what follows it, and `pacman -Syu` is a shell script that sleeps in a child
process and says, in files, whether it was started, interrupted or let finish. Only the network, the
machine's own health reading and the pacman lock path are replaced; signal delivery, the wait on the child,
the report, the lock and the exit status are the real ones. Nothing here reaches the real pacman or sudo.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

DRIVER = """
import os, sys
from pathlib import Path
from arch_updater import auto, main, repairs, restarts, watch
from arch_updater.shell import Shell

def offline(self, url, limit=1 << 20, timeout=30):
    raise OSError("tests are offline")

Shell.get = offline
Shell.resolves = lambda self, host="archlinux.org": True
Shell.free_bytes = lambda self, path: 100 * 1024**3
repairs.PACMAN_LOCK = Path(os.environ["TEST_LOCK"])
restarts.PROC = Path(os.environ["TEST_PROC"])
watch.snapshot = lambda **kwargs: {"pacnew": []}
watch.attest = lambda **kwargs: {"issues": []}
auto.scan_cves = lambda: {"available": False}
sys.exit(main.main(["auto"]))
"""

PACMAN = """#!/bin/sh
case "$1" in
  -Syu)
    echo $$ > "$MARK/pacman.pid"
    trap 'echo stopped > "$MARK/int"; exit 130' INT
    i=0
    while [ $i -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
    echo done > "$MARK/finished"
    exit 0 ;;
  -Q) echo "a 1" ;;
  -Qqm|-Qu) exit 1 ;;
  *) exit 0 ;;
esac
"""
SUDO = '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n'
FLATPAK = '#!/bin/sh\necho "$@" >> "$MARK/flatpak"\n'
TAR = "#!/bin/sh\nexit 0\n"  # the record of /etc: not this test's business, and the real /etc is not read


@pytest.fixture
def machine(tmp_path):
    bin_dir, mark, home = tmp_path / "bin", tmp_path / "mark", tmp_path / "home"
    for folder in (bin_dir, mark, home / ".config/arch-updater", tmp_path / "proc"):
        folder.mkdir(parents=True, exist_ok=True)
    for name, text in (("pacman", PACMAN), ("sudo", SUDO), ("flatpak", FLATPAK), ("tar", TAR)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    for name in ("sleep", "true"):
        (bin_dir / name).symlink_to(f"/usr/bin/{name}")
    (home / ".config/arch-updater/auto.json").write_text('{"conditions": [], "unattended_aur": false}')
    state = home / ".local/state/arch-updater"
    env = {
        "PATH": str(bin_dir),  # nothing else: no real pacman, sudo, yay, notify-send or systemctl is found
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "ARCH_UPDATER_STATE": str(state),
        "PYTHONPATH": str(REPO),
        "MARK": str(mark),
        "TEST_LOCK": str(tmp_path / "db.lck"),
        "TEST_PROC": str(tmp_path / "proc"),
        "LANG": "C.UTF-8",
    }

    def start() -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, "-c", DRIVER], env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

    def pacman_started(child: subprocess.Popen[str]) -> int:
        deadline = time.monotonic() + 60  # a loaded machine is slow to reach the update
        while time.monotonic() < deadline and child.poll() is None:
            pid = mark / "pacman.pid"
            if pid.is_file() and pid.read_text().strip():
                return int(pid.read_text())
            time.sleep(0.05)
        raise AssertionError(f"pacman -Syu never started: {child.poll()} {child.stderr.read() if child.poll() is not None else ''}")

    class Machine:
        pass

    found = Machine()
    found.start, found.pacman_started, found.mark, found.state = start, pacman_started, mark, state
    return found


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def finish(child: subprocess.Popen[str]) -> tuple[int, dict]:
    out, err = child.communicate(timeout=60)
    report = json.loads(out)  # exactly one JSON document: anything before or after it would not parse
    return child.returncode, report


def flatpak_updated(mark: Path) -> bool:
    calls = (mark / "flatpak").read_text().splitlines() if (mark / "flatpak").is_file() else []
    return "update -y --noninteractive" in calls


def lock_is_free(state: Path) -> bool:
    with (state / "auto.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def test_ctrl_c_reaches_the_package_command_through_its_session_and_the_run_ends_as_130(machine) -> None:
    child = machine.start()
    pacman = machine.pacman_started(child)
    child.send_signal(signal.SIGINT)  # to the updater alone, as `kill -INT <pid>` does
    code, report = finish(child)
    assert code == 130 and report["exit_code"] == 130 and report["state"] == "INTERRUPTED"
    assert report["signal"] == signal.SIGINT and report["child_stopped"] is True
    assert (machine.mark / "int").is_file() and not (machine.mark / "finished").exists(), "pacman was told and stopped by itself"
    assert gone(pacman), "no process of the update is left behind"
    with pytest.raises(ProcessLookupError):
        os.killpg(pacman, 0)  # nor anything else in the session it ran in
    assert not flatpak_updated(machine.mark), "no other store was started"
    assert lock_is_free(machine.state)
    assert json.loads((machine.state / "last-auto.json").read_text())["state"] == "INTERRUPTED"


def test_sigterm_lets_the_package_command_finish_and_starts_nothing_else(machine) -> None:
    child = machine.start()
    pacman = machine.pacman_started(child)
    child.send_signal(signal.SIGTERM)
    code, report = finish(child)
    assert code == 143 and report["state"] == "INTERRUPTED" and report["signal"] == signal.SIGTERM
    assert report["child_stopped"] is False, "it ended on its own, with 0"
    assert (machine.mark / "finished").is_file() and not (machine.mark / "int").exists(), "the transaction was never interrupted"
    assert gone(pacman)
    assert not flatpak_updated(machine.mark), "the next store was not started"
    assert [item["detail"] for item in report["trouble"]] == ["stopped by SIGTERM"]
    assert lock_is_free(machine.state)


def test_without_a_signal_the_same_tree_runs_through(machine) -> None:
    """The harness itself is sound: left alone, the update finishes, the next store runs, and the exit is 0."""
    child = machine.start()
    code, report = finish(child)
    assert code == 0 and report["state"] == "IDLE", report["trouble"]
    assert (machine.mark / "finished").is_file() and flatpak_updated(machine.mark)
