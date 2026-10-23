"""Red flags in a PKGBUILD and its .install, read before an unattended AUR build.

The files are read as text and never sourced, so nothing in them runs here. A
flag does not prove harm; it holds the package for a person, who reads the file
and updates it by hand. A false alarm costs one `yay -Sua`; a miss costs the
machine, so every pattern leans to flagging. What is looked for:

  - a network tool run as a command (curl, wget, nc, ncat, netcat, socat): the
    sources are makepkg's to fetch, and a build that fetches more is not reviewed;
  - a request for privileges (sudo, doas, pkexec, run0, su): a build runs as the
    account and has no business asking;
  - code hidden from a reader: `eval`, or base64 / xxd / openssl decoding;
  - a command substitution, `$(...)` or backticks, outside every function: it
    runs as soon as the file is read, before any review a helper offers;
  - an invisible character (zero width, a direction override, a soft hyphen)
    anywhere, or anything that is not ASCII in the fields that name packages,
    sources and the upstream URL, where a letter from another alphabet makes a
    look-alike name;
  - a local source file that is binary (an ELF program, or any file with a NUL
    byte): a program shipped in the AUR checkout instead of built from source.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Protocol

PLAIN = "https://aur.archlinux.org/cgit/aur.git/plain/{file}?h={base}"
BASE = re.compile(r"[a-z0-9@._+-]+")
MAX_LOCAL = 40  # local source files read per package; a package with more is held unread
NETWORK = ("curl", "wget", "nc", "ncat", "netcat", "socat")
PRIVILEGE = ("sudo", "doas", "pkexec", "run0", "su")
# Where a word is a command: the start of a line or of a pipeline, a list, a subshell or a substitution, after a
# keyword, or after a wrapper that runs what follows it. A path in front of the name (/usr/bin/curl) is the same.
_START = r"""(?:^|[;&|{`!]|(?<!=)\(|\$\(|\b(?:then|do|else|elif|exec|command|env|nohup|xargs|time|builtin)\s)\s*(?:\w+=(?:"[^"\n]*"|'[^'\n]*'|(?!\()\S*)\s+)*(?:/[\w./-]*/)?"""
_END = r"(?=[\s;&|)`]|$)"
COMMAND = {
    "network": re.compile(_START + r"(" + "|".join(NETWORK) + r")" + _END, re.MULTILINE),
    "privilege": re.compile(_START + r"(" + "|".join(PRIVILEGE) + r")" + _END, re.MULTILINE),
    "eval": re.compile(_START + r"(eval)" + _END, re.MULTILINE),
}
DECODE = re.compile(r"\b(base64\b[^\n]*?\s(?:-[A-Za-z]*d[A-Za-z]*|--decode|-D)\b|xxd\b[^\n]*?\s-r\b|openssl\b[^\n]*?\bbase64\b[^\n]*?\s-d\b|openssl\b[^\n]*?\s-d\b[^\n]*?\bbase64\b)")
INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
NAME_FIELDS = ("pkgname", "pkgbase", "depends", "makedepends", "checkdepends", "optdepends", "provides", "conflicts", "replaces", "source", "url", "install")
_FIELD = re.compile(r"^[ \t]*(" + "|".join(NAME_FIELDS) + r")(?:_\w+)?\+?=(\([^)]*\)|\"[^\"\n]*\"|'[^'\n]*'|[^\s;]*)", re.MULTILINE)
_SCALAR = re.compile(r"^(\w+)=(\"[^\"$`\n]*\"|'[^'\n]*'|[^\s\"'$`();]+)\s*$", re.MULTILINE)
_FUNCTION = re.compile(r"^\s*(?:function\s+)?[\w.+-]+\s*\(\)\s*\{?\s*$|^\s*function\s+[\w.+-]+\s*\{?\s*$")


class Getter(Protocol):
    def get(self, url: str, limit: int = ..., timeout: float = ...) -> str: ...


def _code(text: str) -> list[tuple[int, str]]:
    """The lines a shell would run, numbered, with whole-line comments dropped."""
    return [(number, line) for number, line in enumerate(text.splitlines(), 1) if not line.lstrip().startswith("#")]


def top_level(text: str) -> list[tuple[int, str]]:
    """The lines outside every function body: they run when the file is read."""
    depth, outside = 0, []
    for number, line in _code(text):
        if depth == 0 and not _FUNCTION.match(line):
            outside.append((number, line))
        opened = len(re.findall(r"(?<!\$)\{", line))
        closed = line.count("}") - line.count("${")
        depth = max(0, depth + opened - closed)
    return outside


def scan(text: str, name: str = "PKGBUILD") -> list[str]:
    """Every red flag in one file, each as a reason a person can check: `PKGBUILD line 12: runs curl`."""
    found: list[str] = []
    code = "\n".join(line for _, line in _code(text))
    numbers = [number for number, _ in _code(text)]
    for kind, pattern in COMMAND.items():
        for match in pattern.finditer(code):
            line = numbers[code.count("\n", 0, match.start(1))]
            verb = {"network": "runs the network tool", "privilege": "asks for privileges with", "eval": "runs a string as code with"}[kind]
            found.append(f"{name} line {line}: {verb} {match.group(1)}")
    for number, line in _code(text):
        decoded = DECODE.search(line)
        if decoded:
            found.append(f"{name} line {number}: decodes hidden data ({decoded.group(1).split()[0]})")
    for number, line in top_level(text):
        if re.search(r"\$\((?!\()|`", line):
            found.append(f"{name} line {number}: runs a command substitution outside any function, as soon as the file is read")
    for number, line in enumerate(text.splitlines(), 1):
        for char in INVISIBLE.findall(line):
            found.append(f"{name} line {number}: an invisible character {_named(char)}")
    for match in _FIELD.finditer(text):
        value = match.group(2)
        if match.group(1) == "optdepends":  # a description may be in any language; the name before ":" may not
            value = " ".join(word.strip("'\"").split(":")[0] for word in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", value.strip("()")))
        foreign = sorted({char for char in value if ord(char) > 127 and not INVISIBLE.match(char)})
        if foreign:
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{name} line {line}: {match.group(1)} holds a character that is not ASCII, {', '.join(_named(char) for char in foreign)}")
    return sorted(set(found), key=found.index)


def _named(char: str) -> str:
    return f"U+{ord(char):04X} {unicodedata.name(char, 'UNNAMED')}"


def binary(content: str) -> bool:
    """An ELF program or any file with a NUL byte, as https text decoding hands it over."""
    return content.startswith("\x7fELF") or "\x00" in content


def _values(text: str, field: str) -> list[str]:
    values = []
    for match in _FIELD.finditer(text):
        if match.group(1) == field:
            raw = re.sub(r"(?m)(?:^|(?<=\s))#[^\n]*", "", match.group(2).strip("()"))  # a comment inside an array
            values += [word.strip("'\"") for word in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", raw)]
    return values


def _expand(word: str, scalars: dict[str, str]) -> str:
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda match: scalars.get(match.group(1) or match.group(2), match.group(0)), word)


def local_files(text: str) -> tuple[list[str], list[str]]:
    """(the local files the PKGBUILD names: sources without a URL and its .install, names it could not resolve)."""
    scalars = {key: value.strip("'\"") for key, value in _SCALAR.findall(text)}
    names, unknown = [], []
    for word in [*_values(text, "source"), *_values(text, "install")]:
        target = word.split("::", 1)[1] if "::" in word else word
        if "://" in target or not target:
            continue
        local = _expand(word.split("::", 1)[0] if "::" in word else word, scalars)
        (unknown if "$" in local or "/" in local or local in ("", ".", "..") else names).append(local)
    return list(dict.fromkeys(names)), unknown


def review(sh: Getter, base: str) -> list[str]:
    """Read the AUR checkout of `base` as it is now and return every red flag; reading failures are flags too."""
    if not BASE.fullmatch(base):
        return [f"a package base that is not a plain name: {base!r}"]

    def fetch(file: str) -> str:
        return sh.get(PLAIN.format(file=urllib.parse.quote(file), base=urllib.parse.quote(base)), limit=1 << 20, timeout=30)

    try:
        pkgbuild = fetch("PKGBUILD")
    except (OSError, ValueError) as exc:
        return [f"the PKGBUILD could not be read ({exc})"]
    found = scan(pkgbuild)
    names, unknown = local_files(pkgbuild)
    found += [f"a local source whose name could not be worked out: {name}" for name in unknown]
    if len(names) > MAX_LOCAL:
        return [*found, f"more than {MAX_LOCAL} local source files: not read"]
    installs = set(_values(pkgbuild, "install"))
    for name in names:
        try:
            content = fetch(name)
        except (OSError, ValueError) as exc:
            found.append(f"the local file {name} could not be read ({exc})")
            continue
        if binary(content):
            found.append(f"{name} is a binary file among the sources")
        elif name in installs or name.endswith(".install"):
            found += scan(content, name)
    return found
