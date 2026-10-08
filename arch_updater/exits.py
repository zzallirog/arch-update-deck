"""How an unattended run can end. The number is the process exit status."""

import signal

IDLE, READY_FOR_REBOOT, BLOCKED, RECOVERY_PENDING = 0, 10, 20, 30
NETWORK_BLOCKED, BACKUP_BLOCKED, TOOL_FAILURE, SKIPPED = 40, 50, 70, 80
INTERRUPTED = 130  # Ctrl-C: the shell's own convention, 128 + SIGINT; a run ended by SIGTERM or SIGHUP is 143 or 129
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
NAMES = {
    IDLE: "IDLE",
    READY_FOR_REBOOT: "READY_FOR_REBOOT",
    BLOCKED: "BLOCKED",
    RECOVERY_PENDING: "RECOVERY_PENDING",
    NETWORK_BLOCKED: "NETWORK_BLOCKED",
    BACKUP_BLOCKED: "BACKUP_BLOCKED",
    TOOL_FAILURE: "TOOL_FAILURE",
    SKIPPED: "SKIPPED",
    **{128 + number: "INTERRUPTED" for number in STOP_SIGNALS},
}
# The exits a configured condition may claim; anything else would let a "no" pass as success.
CONDITION = (BLOCKED, NETWORK_BLOCKED, BACKUP_BLOCKED)


class Stopped(KeyboardInterrupt):
    """Ctrl-C, SIGTERM or SIGHUP taken the way Ctrl-C is: the screen is put back, nothing is killed.

    `signum` is the signal that arrived. `child_stopped` is what is known of the command that was running:
    True if it ended by a signal, False if it ended on its own, None if no command was running.
    """

    def __init__(self, signum: int = signal.SIGINT, child_stopped: bool | None = None) -> None:
        super().__init__()
        self.signum = signum
        self.child_stopped = child_stopped


def interrupted(code: object) -> bool:
    """Is this exit status the one of a run that a signal ended?"""
    return code in {128 + number for number in STOP_SIGNALS}


def ended_by_signal(returncode: int, sent: bool = False) -> bool:
    """Did a command end because a signal reached it, judging by how it ended and not by what it printed?

    A command that was sent a signal from here and ended with a failure was stopped by it. One that nobody sent
    anything to (the terminal may have, or the signal only reached us) is stopped only if its status is a signal's:
    killed by it, or the shell's 128 + number, or the bare number pacman exits with.
    """
    if returncode == 0:
        return False
    return sent or any(returncode in (-number, 128 + number) or (returncode == number and number != signal.SIGHUP) for number in STOP_SIGNALS)
