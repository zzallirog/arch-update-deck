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


HIDDEN = "<redacted>"
# The user part of a URL (`https://user:password@host`, or a token given as the user) and query values with a
# secret's name. Mirror URLs and build output carry them now and then; a brief, a case or a share must not.
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/?#@\s]+@")
_SECRET_QUERY = re.compile(
    r"(?i)([?&;](?:access_token|private_token|auth_token|token|api_key|apikey|key|password|passwd|pass|pwd|secret|client_secret"
    r"|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&#\s'\"]+"
)


def secrets(text: str) -> str:
    """The text with credentials in URLs replaced by <redacted>; the host, path and other parameters stay."""
    return _SECRET_QUERY.sub(rf"\1{HIDDEN}", _USERINFO.sub(rf"\1{HIDDEN}@", str(text)))
