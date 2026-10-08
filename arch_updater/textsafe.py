"""Text from outside, made safe to print on a terminal.

Package names, versions, build output and logs are written by other people. A terminal obeys the escape sequences
in them: a title, a cleared screen, the clipboard (OSC 52). Anything that reaches the screen goes through clean().
"""

from __future__ import annotations

import re

# Sequences that carry a payload or a command, in both the 7-bit (ESC ...) and the 8-bit (C1) forms.
_SEQUENCE = re.compile(
    r"\x1b\][^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"  # OSC: a title, a hyperlink, the clipboard; ends at BEL or ST
    r"|\x9d[^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"  # OSC, 8-bit
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c\n]*(?:\x1b\\|\x9c)?"  # DCS, SOS, PM, APC: to ST
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"  # CSI: colours, cursor moves, erase
    r"|\x1b[ -/]*[0-~]?"  # any other escape: ESC c (reset), ESC ( B ...
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0 and C1 except tab and newline


def clean(text: str) -> str:
    """The text without any escape sequence and without any control character but newline and tab."""
    return _CONTROL.sub("", _SEQUENCE.sub("", str(text)))


_EXIT_LINE = re.compile(r"\[exit -?\d+\]")


def cause(detail: str) -> str:
    """The line of a failed command's output that says why: the last one with words in it, not the `[exit N]` the log adds."""
    lines = [line.strip() for line in str(detail).splitlines()]
    return next((line for line in reversed(lines) if line and not _EXIT_LINE.fullmatch(line)), "")
