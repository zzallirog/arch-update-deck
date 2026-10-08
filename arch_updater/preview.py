from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import __version__
from .engine import (
    CONFIG_PATH,
    HARD_FREE_BYTES,
    STATE_ROOT,
    load_json,
    planned_update_commands,
    privilege_command,
    run_capture,
)


HUD_CACHE_PATH = STATE_ROOT / "hud-cache.json"
DECK_BOOK_PATH = STATE_ROOT / "deck-book.json"
DECK_BOOK_LOG_PATH = STATE_ROOT / "deck-book.log"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

CYAN = "38;5;110"
PINK = "38;5;180"
VIOLET = "38;5;103"
GREEN = "38;5;108"
GOLD = "38;5;187"
DIM = "2"
INK = "38;5;252"
MUTE = "38;5;245"
SLOT_WIDTH = 46

ACTION_SLOTS: list[tuple[str, str, str]] = [
    ("help", "Help", "what the program can do"),
    ("engine", "Updater Engine", "default: repo → AUR → check"),
    ("store", "Store", "repo + AUR storefront · type a package"),
    ("_sep_look", "look", ""),
    ("runway", "Runway", "is it safe to update now"),
    ("status", "Status", "kernel, disk, GPU, queue, ABI"),
    ("pending", "Pending", "repo and AUR update queue"),
    ("plan", "Plan", "what to update, when and in which mode"),
    ("last", "Last run", "outcome and log of the previous run"),
    ("log", "Log", "latest installs from pacman.log"),
    ("orphans", "Orphans", "packages nothing depends on"),
    ("news", "News", "archlinux.org/news feed"),
    ("cve", "CVE", "vulnerabilities per Arch security tracker"),
    ("schedule", "Schedule", "read-only patrol · never installs"),
    ("_sep_apply", "update", ""),
    ("repo", "Repos", "pacman -Syu"),
    ("aur", "AUR", "yay -Sua"),
    ("full", "Full", "repo + AUR + check · same as Engine"),
    ("kernel", "Kernel", "install a kernel and set GRUB"),
    ("_sep_health", "system", ""),
    ("attest", "Attest", "health, pacnew, is a reboot needed"),
    ("pacnew", "Pacnew", "diff of stock configs"),
    ("units", "Failed units", "failed systemd services"),
    ("vault", "Vault", "known failures and how to fix them"),
    ("_sep_log", "archive", ""),
    ("share", "Share report", "sanitized report · preview first"),
    ("sessions", "Sessions", "installs from Codex/Claude chats"),
    ("exit", "Exit", "exit"),
]

WHY: dict[str, str] = {
    "help": "Capability map. What the deck can do on this machine.",
    "engine": "Normal update run: official repo first, then AUR, then a health check. A separate confirmation always comes before pacman and yay.",
    "store": "Storefront for two sources: official repo and AUR. You can type a package name right away; search shows a card and does not start an install.",
    "runway": "One answer from live Status and Attest: reboot-first, blocked or ready-to-review; the queue and the next read-only step.",
    "status": "Snapshot: which kernel is running, free space, GPU, package queue, Hyprland ABI, failed units.",
    "pending": "Packages with a new version. repo is official Arch, AUR is aur.archlinux.org. Enter opens the package description.",
    "plan": "Map of the default update: when to run Engine, when to split repo and AUR, and what Schedule does without you.",
    "last": "Previous run of this program: mode, exit code, classified failures, tail of run.log.",
    "log": "Live /var/log/pacman.log: latest upgraded/installed/removed.",
    "orphans": "pacman -Qtd — packages nothing depends on. Candidates for manual removal.",
    "news": "Latest Arch Linux News headlines. Network, timeout 3s, no new dependencies.",
    "cve": "arch-audit checks installed packages against the security tracker. Read-only.",
    "schedule": "read-only patrol · never installs. The daily patrol checks the queue, health and CVE and saves a snapshot. It only runs read-only queries: no installs, no sudo, no reboot; Full stays your action.",
    "repo": "Update the official repositories: sudo pacman -Syu. Interactive, on a tty.",
    "aur": "Update AUR: yay -Sua. Builds can take a long time.",
    "full": "Repos, then AUR, then Attest. One pass that updates the machine.",
    "kernel": "Install the chosen profile (package + headers) and set GRUB_DEFAULT from the live grub.cfg.",
    "attest": "Post-update summary: kernel modules, Hyprland ABI, DKMS, pacnew, new failed units, whether a reboot is needed.",
    "pacnew": "The stock file sits next to yours as .pacnew. Enter shows the diff.",
    "units": "systemctl --failed. Enter shows the unit status.",
    "vault": "Catalog of failures the deck has already seen, and what to do about them.",
    "share": "Shows the sanitized allowlist summary of the last patrol before manual handoff. Creates no file, copies and sends nothing.",
    "sessions": "Parse Codex and Claude logs: where packages were actually installed.",
    "exit": "Close the menu.",
}


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


UNKNOWN_SOURCE = "unknown"


def render_status_badge(payload: dict[str, Any], field: str) -> str:
    """One status badge: names the live source field, or `unknown` if it is absent.

    Missing `field` never paints green and never infers repo, AUR, or READY.
    """
    if field not in payload:
        return paint(MUTE, UNKNOWN_SOURCE) + paint(DIM, f" · {field}")
    value = payload[field]
    label, code = _status_badge_claim(field, value)
    return paint(code, label) + paint(DIM, f" · {field}")


def _status_badge_claim(field: str, value: Any) -> tuple[str, str]:
    if field == "source":
        kind = str(value).strip().lower()
        if kind == "aur":
            return "AUR", PINK
        if kind == "repo":
            return "repo", CYAN
        if kind:
            return kind, MUTE
        return UNKNOWN_SOURCE, MUTE
    if field == "mutates":
        if value:
            return "updates", f"1;{GOLD}"
        return "shows", f"1;{GREEN}"
    if field == "reboot_required":
        if value:
            return "REBOOT FIRST", f"1;{INK}"
        return "no reboot", MUTE
    if field == "pacman_lock":
        if value:
            return "BLOCKED", f"1;{INK}"
        return "no lock", MUTE
    if value is None or value == "":
        return UNKNOWN_SOURCE, MUTE
    return str(value), MUTE


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def marquee(text: str, width: int = 46, speed: float = 7.0, now: float | None = None) -> str:
    compact = " ".join(text.split())
    if not compact:
        compact = "no pending yet"
    if visible_len(compact) <= width:
        return compact + " " * (width - visible_len(compact))
    stream = compact + "   ·   "
    idx = int((now if now is not None else time.time()) * speed) % len(stream)
    doubled = stream + stream
    return doubled[idx : idx + width]


def render_slot(
    title: str,
    subtitle: str,
    *,
    mutates: bool = False,
    separator: bool = False,
    width: int = SLOT_WIDTH,
) -> str:
    if separator:
        return paint(MUTE, f"  {title}")
    rail = paint(GOLD, "·") if mutates else paint(MUTE, "·")
    heading = paint(f"1;{GOLD}", title) if mutates else paint(f"1;{INK}", title)
    why = paint(MUTE, subtitle)
    return f"  {rail} {heading}\n    {why}"


def menu_fzf_records() -> list[str]:
    mutating = {"engine", "repo", "aur", "full", "kernel"}
    cache = load_hud_cache()
    rows: list[str] = []
    skipped_leading_sep = False
    for item_id, title, subtitle in ACTION_SLOTS:
        if not skipped_leading_sep and item_id.startswith("_sep"):
            skipped_leading_sep = True
            continue
        shown = render_slot(
            title,
            _live_subtitle(item_id, subtitle, cache),
            mutates=item_id in mutating,
            separator=item_id.startswith("_sep"),
        )
        rows.append(f"{item_id}\taction\t{shown}")
    return rows


def _package_record_lists(payload: dict[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    updates = payload.get("updates") if isinstance(payload.get("updates"), dict) else {}

    def pick(key: str) -> list[str] | None:
        if key in payload:
            value = payload.get(key)
            return list(value) if isinstance(value, list) else None
        if key in updates:
            value = updates.get(key)
            return list(value) if isinstance(value, list) else None
        return None

    return pick("repo_packages"), pick("aur_packages")


def queue_counts_from_records(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """Count the queue from parsed package records, never from HUD/header numbers."""
    repo_records, aur_records = _package_record_lists(payload)
    repo_n = None if repo_records is None else len(repo_records)
    aur_n = None if aur_records is None else len(aur_records)
    return repo_n, aur_n


def format_queue_counts(repo_n: int | None, aur_n: int | None) -> str:
    repo_s = UNKNOWN_SOURCE if repo_n is None else str(repo_n)
    aur_s = UNKNOWN_SOURCE if aur_n is None else str(aur_n)
    return f"{repo_s} repo · {aur_s} AUR"


def _queue_label(cache: dict[str, Any]) -> str | None:
    repo_n, aur_n = queue_counts_from_records(cache)
    if repo_n is None and aur_n is None:
        if "hud" in cache or "repo" in cache or "aur" in cache:
            return format_queue_counts(None, None)
        return None
    return format_queue_counts(repo_n, aur_n)


def _live_subtitle(item_id: str, default: str, cache: dict[str, Any]) -> str:
    queue = _queue_label(cache)
    if item_id == "pending" and queue:
        return queue
    if item_id == "runway":
        if cache.get("lock"):
            return "blocked · pacman lock"
        if queue:
            return queue
    if item_id == "status" and queue:
        kernel = cache.get("kernel")
        head = str(kernel) if kernel else "kernel"
        return f"{head} · {queue}"
    if item_id == "pacnew":
        count = cache.get("pacnew")
        if isinstance(count, int) and count:
            return f"{count} files · diff of stock configs"
    return default


def _pkg_record(source: str, name: str, label: str, tag: str) -> str:
    color = PINK if source.strip().lower() == "aur" else CYAN
    shown = f"{paint(color, f'{tag:<8}')} {label}"
    return f"pkg:{source}:{name}\tpkg\t{shown}"


def _name_rank(name: str, needle: str) -> int:
    n = name.lower()
    if n == needle:
        return 0
    if n.startswith(needle) or n.endswith("-" + needle):
        return 1
    if needle in n.split("-"):
        return 2
    if needle in n:
        return 3
    return 5


def package_fzf_records(query: str) -> list[str]:
    needle = query.strip().lower()
    if not needle:
        return []
    terms = needle.split()
    ranked: list[tuple[int, str, str]] = []
    seen: set[str] = set()

    def add(source: str, name: str, label: str, tag: str) -> None:
        key = f"{source}:{name}"
        if not name or key in seen:
            return
        haystack = f"{name} {label}".lower()
        if any(term not in haystack for term in terms):
            return
        seen.add(key)
        ranked.append((_name_rank(name, terms[0]), name, _pkg_record(source, name, label, tag)))

    cache = load_hud_cache()
    for line in cache.get("repo_packages") or []:
        add("repo", line.split()[0], line, "repo")
    for line in cache.get("aur_packages") or []:
        add("aur", line.split()[0], line, "AUR")
    try:
        for profile in load_json(CONFIG_PATH).get("kernels") or []:
            package = profile.get("package")
            if not package:
                continue
            add("kernel", package, f"{profile.get('label', package)} · {package}", "kernel")
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    if len(needle) >= 2:
        queries = [query.strip()] if len(terms) == 1 else terms
        for search_query in queries:
            result = run_capture(["pacman", "-Ss", "--", search_query], timeout=12)
            current_name = ""
            current_label = ""
            for raw in result.output.splitlines():
                if raw.startswith(" "):
                    continue
                fields = raw.split()
                if not fields or "/" not in fields[0]:
                    continue
                current_name = fields[0].split("/")[-1]
                current_label = raw
                add("repo", current_name, current_label, "repo")
                if len(ranked) >= 80:
                    break
            if len(ranked) >= 40:
                break
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in ranked[:40]]


def fzf_records_for_query(query: str) -> str:
    query = query.strip()
    rows = menu_fzf_records() if not query else package_fzf_records(query)
    if query and not rows:
        empty = paint("38;5;243", f"no packages for «{query}»")
        rows = [f"_\tnone\t{empty}"]
    return "\0".join(rows) + "\0"


STOREFRONT_SLOTS: list[tuple[str, str, str]] = [
    ("store:repo", "Official repo", "signed Arch/CachyOS packages"),
    ("store:aur", "AUR", "community PKGBUILD · mind the source"),
    ("store:pending", "Your update shelf", "what is already waiting on this machine"),
    ("store:back", "Back", "back to the Deck"),
]


def storefront_fzf_records(query: str) -> str:
    query = query.strip()
    if query:
        rows = package_fzf_records(query)
    else:
        cache = load_hud_cache()
        rows = [
            f"_sep_store_repo\tstore\t{render_slot('Official repo', 'signed Arch/CachyOS packages')}",
        ]
        repo_lines = list(cache.get("repo_packages") or [])[:24]
        if repo_lines:
            rows.extend(_pkg_record("repo", line.split()[0], line, "repo") for line in repo_lines)
        else:
            rows.append(f"_\tnone\t{paint(MUTE, '  type a package name · pacman -Ss')}")
        rows.append(
            f"_sep_store_aur\tstore\t{render_slot('AUR', 'community PKGBUILD · aur.archlinux.org')}"
        )
        aur_lines = list(cache.get("aur_packages") or [])[:24]
        if aur_lines:
            rows.extend(_pkg_record("aur", line.split()[0], line, "AUR") for line in aur_lines)
        else:
            rows.append(f"_\tnone\t{paint(MUTE, '  AUR comes from your update shelf')}")
        rows.append(
            f"_sep_store_shelf\tstore\t{render_slot('Your update shelf', 'repo + AUR queue · inspect-only')}"
        )
    if query and not rows:
        rows = [f"_\tnone\t{paint('38;5;243', f'no packages for «{query}»')}"]
    return "\0".join(rows) + "\0"


def render_storefront(state: dict[str, Any]) -> str:
    repo_n, aur_n = queue_counts_from_records(state)
    repo_s = UNKNOWN_SOURCE if repo_n is None else str(repo_n)
    aur_s = UNKNOWN_SOURCE if aur_n is None else str(aur_n)
    lines = [
        " " + paint(f"1;{INK}", "Two sources. One machine."),
        " " + paint(MUTE, "repo  · signed Arch/CachyOS packages"),
        " " + paint(MUTE, "AUR   · community PKGBUILD, not the same trust boundary"),
        "",
        " " + paint(f"1;{CYAN}", "Type a package name right here"),
        " " + paint(MUTE, "card first; Repos/AUR/Full are a separate checkout"),
        " " + paint(MUTE, "Your update shelf · inspect-only"),
        "",
        " " + paint(CYAN, f"in your shelf  {repo_s} repo"),
        " " + paint(PINK, f"              {aur_s} AUR"),
    ]
    return _box("storefront", lines, width=62, color=CYAN) + "\n"


def render_store_source(source: str, state: dict[str, Any]) -> str:
    repo_n, aur_n = queue_counts_from_records(state)
    if source == "aur":
        title = "AUR · community shelf"
        color = PINK
        aur_s = UNKNOWN_SOURCE if aur_n is None else str(aur_n)
        lines = [
            " " + paint(f"1;{INK}", "AUR is a PKGBUILD source, not a trust signal."),
            " " + paint(MUTE, "Card, deps and source first; then a separate decision."),
            "",
            " " + paint(PINK, f"in queue  {aur_s} AUR"),
            " " + paint(MUTE, "search here shows AUR from your update shelf"),
        ]
    else:
        title = "Official repo · signed shelf"
        color = CYAN
        repo_s = UNKNOWN_SOURCE if repo_n is None else str(repo_n)
        lines = [
            " " + paint(f"1;{INK}", "Repo is the official catalog of Arch/CachyOS."),
            " " + paint(MUTE, "Typing a name searches; the card explains before checkout."),
            "",
            " " + paint(CYAN, f"in queue  {repo_s} repo"),
            " " + paint(MUTE, "Repos/Full update the whole queue, not one storefront click"),
        ]
    return _box(title, lines, width=62, color=color) + "\n"


def load_hud_cache() -> dict[str, Any]:
    if not HUD_CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(HUD_CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_deck_book(cards: dict[str, str], duration_seconds: float) -> dict[str, Any]:
    """Atomically persist cards. Keep the last valid book if the replace fails."""
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = {"generated_at": generated_at, "duration_seconds": round(duration_seconds, 2), "cards": cards}
    DECK_BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = DECK_BOOK_PATH.read_bytes() if DECK_BOOK_PATH.is_file() else None
    temporary = DECK_BOOK_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        temporary.replace(DECK_BOOK_PATH)
    except OSError:
        if previous is not None:
            DECK_BOOK_PATH.write_bytes(previous)
        raise
    with DECK_BOOK_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(f"{generated_at} deck-book {duration_seconds:.2f}s cards={','.join(sorted(cards))}\n")
    return payload


def load_deck_book_card(action_id: str) -> str | None:
    try:
        payload = json.loads(DECK_BOOK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    card = (payload.get("cards") or {}).get(action_id)
    return card if isinstance(card, str) else None


def render_deck_book_preview(action_id: str) -> str:
    """Prefer the pre-rendered card; never make fzf wait on a system probe."""
    return load_deck_book_card(action_id) or render_action_preview(action_id)


def render_book_page(title: str, text: str, subtitle: str) -> str:
    lines = [" " + paint(f"1;{INK}", subtitle), ""]
    lines.extend(" " + line for line in text.rstrip().splitlines())
    return _box(title, lines, width=74, color=CYAN) + "\n"


def write_hud_cache(state: dict[str, Any], last_run: dict[str, Any] | None, hud: str) -> dict[str, Any]:
    previous = load_hud_cache()
    updates = state.get("updates") or {}

    def _records(key: str, count_key: str) -> list[str] | None:
        previous_records = list(previous.get(key) or [])[:80] if key in previous else None
        if key in updates:
            incoming = list(updates.get(key) or [])[:80]
            if incoming:
                return incoming
            if updates.get(count_key) is None and previous_records:
                return previous_records
            return incoming
        if updates.get(count_key) is None and previous_records is not None:
            return previous_records
        return None

    repo_packages = _records("repo_packages", "repo")
    aur_packages = _records("aur_packages", "aur")
    repo = None if repo_packages is None else len(repo_packages)
    aur = None if aur_packages is None else len(aur_packages)
    stored_repo_packages = [] if repo_packages is None else repo_packages
    stored_aur_packages = [] if aur_packages is None else aur_packages
    ticker_parts: list[str] = []
    for line in stored_repo_packages[:24]:
        ticker_parts.append(f"repo {line}")
    for line in stored_aur_packages[:24]:
        ticker_parts.append(f"aur {line}")
    if last_run:
        status = "FAILED" if last_run.get("failed") else "OK"
        ticker_parts.append(f"last {status} {last_run.get('mode')}")
    payload = {
        "hud": hud,
        "ticker": "  ·  ".join(ticker_parts),
        "repo": repo,
        "aur": aur,
        "repo_packages": None if repo_packages is None else stored_repo_packages,
        "aur_packages": None if aur_packages is None else stored_aur_packages,
        "kernel": (state.get("kernel") or {}).get("running_package") or previous.get("kernel"),
        "lock": bool(state.get("pacman_lock")),
        "pacnew": len(state.get("pacnew") or []),
        "updated_at": time.time(),
    }
    HUD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    HUD_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def _wrap(text: str, width: int = 48) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if len(trial) > width and current:
            lines.append(" " + current)
            current = word
        else:
            current = trial
    if current:
        lines.append(" " + current)
    return lines or [" "]


def _box(title: str, lines: list[str], width: int = 56, color: str = CYAN) -> str:
    label = f" {title} "
    inner = width - 2
    pad = max(0, inner - visible_len(label) - 1)
    top = paint(color, "┌─" + label + "─" * pad + "┐")
    body: list[str] = []
    for line in lines:
        gap = inner - visible_len(line)
        if gap < 0:
            stripped = ANSI_RE.sub("", line)
            line = stripped[: inner - 1] + "…"
            gap = inner - visible_len(line)
        body.append(paint(color, "│") + line + " " * gap + paint(color, "│"))
    bottom = paint(color, "└" + "─" * inner + "┘")
    return "\n".join([top, *body, bottom])


def _host() -> str:
    return os.uname().nodename.split(".")[0]


def _prompt_line(command: str, mutates: bool) -> str:
    user = os.environ.get("USER", "user")
    host = _host()
    prefix = paint(DIM, f"{user}@{host}") + " " + paint(CYAN, "▸")
    painted = paint(PINK if mutates else GREEN, command)
    return f" {prefix} {painted}"


def _lamp(ok: bool, warn: bool = False) -> str:
    if ok:
        return paint(GREEN, "●")
    if warn:
        return paint(GOLD, "●")
    return paint(PINK, "●")


def action_catalog() -> dict[str, dict[str, Any]]:
    privileged = privilege_command()
    repo = " ".join([privileged, "pacman", "-Syu"])
    aur = " ".join(["yay", "-Sua", "--sudo", privileged])
    full_cmds = [" ".join(command) for command in planned_update_commands("full")]
    slots = {item_id: (title, subtitle) for item_id, title, subtitle in ACTION_SLOTS if not item_id.startswith("_sep")}
    catalog = {
        "help": {
            "title": slots["help"][0],
            "command": "capability map",
            "commands": [],
            "blurb": slots["help"][1],
            "why": WHY["help"],
            "mutates": False,
            "confirm": False,
        },
        "engine": {
            "title": slots["engine"][0],
            "command": "Full update · repo → AUR → Attest",
            "commands": full_cmds,
            "blurb": slots["engine"][1],
            "why": WHY["engine"],
            "mutates": True,
            "confirm": True,
        },
        "store": {
            "title": slots["store"][0],
            "command": "browse repo + AUR catalog",
            "commands": ["type package name", "pacman -Si -- $package", "AUR details from the local update shelf"],
            "blurb": slots["store"][1],
            "why": WHY["store"],
            "mutates": False,
            "confirm": False,
        },
        "runway": {
            "title": slots["runway"][0],
            "command": "compose snapshot + attest",
            "commands": [],
            "blurb": slots["runway"][1],
            "why": WHY["runway"],
            "mutates": False,
            "confirm": False,
        },
        "status": {
            "title": slots["status"][0],
            "command": "read-only probe",
            "commands": ["checkupdates", "yay -Qua", "nvidia-smi", "ldd Hyprland"],
            "blurb": slots["status"][1],
            "why": WHY["status"],
            "mutates": False,
            "confirm": False,
        },
        "pending": {
            "title": slots["pending"][0],
            "command": "browse pending",
            "commands": ["pacman -Si $pkg", "yay -Si $pkg"],
            "blurb": slots["pending"][1],
            "why": WHY["pending"],
            "mutates": False,
            "confirm": False,
        },
        "plan": {
            "title": slots["plan"][0],
            "command": "show next argv",
            "commands": full_cmds,
            "blurb": slots["plan"][1],
            "why": WHY["plan"],
            "mutates": False,
            "confirm": False,
        },
        "log": {
            "title": slots["log"][0],
            "command": "tail pacman.log",
            "commands": ["rg '\\[ALPM\\] (upgraded|installed|removed)' /var/log/pacman.log"],
            "blurb": slots["log"][1],
            "why": WHY["log"],
            "mutates": False,
            "confirm": False,
        },
        "orphans": {
            "title": slots["orphans"][0],
            "command": "pacman -Qtd",
            "commands": ["pacman -Qtd"],
            "blurb": slots["orphans"][1],
            "why": WHY["orphans"],
            "mutates": False,
            "confirm": False,
        },
        "news": {
            "title": slots["news"][0],
            "command": "curl archlinux.org/feeds/news/",
            "commands": ["GET https://archlinux.org/feeds/news/"],
            "blurb": slots["news"][1],
            "why": WHY["news"],
            "mutates": False,
            "confirm": False,
        },
        "last": {
            "title": slots["last"][0],
            "command": "read last-run.json",
            "commands": [f"cat {STATE_ROOT / 'last-run.json'}"],
            "blurb": slots["last"][1],
            "mutates": False,
            "confirm": False,
        },
        "repo": {
            "title": slots["repo"][0],
            "command": repo,
            "commands": [repo],
            "blurb": slots["repo"][1],
            "mutates": True,
            "confirm": True,
        },
        "aur": {
            "title": slots["aur"][0],
            "command": aur,
            "commands": [aur],
            "blurb": slots["aur"][1],
            "mutates": True,
            "confirm": True,
        },
        "full": {
            "title": slots["full"][0],
            "command": " && ".join(full_cmds) if full_cmds else repo,
            "commands": full_cmds,
            "blurb": slots["full"][1],
            "mutates": True,
            "confirm": True,
        },
        "kernel": {
            "title": slots["kernel"][0],
            "command": f"{privileged} pacman -S --needed <profile> <headers>",
            "commands": [
                f"{privileged} pacman -S --needed linux-cachyos-bore linux-cachyos-bore-headers",
                "grub-mkconfig -o /boot/grub/grub.cfg",
            ],
            "blurb": slots["kernel"][1],
            "mutates": True,
            "confirm": True,
        },
        "attest": {
            "title": slots["attest"][0],
            "command": "attest",
            "commands": ["dkms status", "pacdiff -o", "systemctl --failed"],
            "blurb": slots["attest"][1],
            "mutates": False,
            "confirm": False,
        },
        "pacnew": {
            "title": slots["pacnew"][0],
            "command": "diff -u file file.pacnew",
            "commands": ["pacdiff -o", "diff -u -- $file $file.pacnew"],
            "blurb": slots["pacnew"][1],
            "mutates": False,
            "confirm": False,
        },
        "units": {
            "title": slots["units"][0],
            "command": "systemctl status --no-pager -n 40",
            "commands": ["systemctl --failed"],
            "blurb": slots["units"][1],
            "mutates": False,
            "confirm": False,
        },
        "vault": {
            "title": slots["vault"][0],
            "command": "open one failure mode",
            "commands": ["filter failure-modes.json"],
            "blurb": slots["vault"][1],
            "mutates": False,
            "confirm": False,
        },
        "share": {
            "title": slots["share"][0],
            "command": "render sanitized local patrol report",
            "commands": [],
            "blurb": slots["share"][1],
            "why": WHY["share"],
            "mutates": False,
            "confirm": False,
        },
        "sessions": {
            "title": slots["sessions"][0],
            "command": "scan transcripts",
            "commands": ["scan ~/.codex/sessions", "scan ~/.claude/projects"],
            "blurb": slots["sessions"][1],
            "mutates": False,
            "confirm": True,
        },
        "exit": {
            "title": slots["exit"][0],
            "command": "return 0",
            "commands": [],
            "blurb": slots["exit"][1],
            "mutates": False,
            "confirm": False,
        },
    }
    for key, item in catalog.items():
        item.setdefault("why", WHY.get(key, item.get("blurb", "")))
    return catalog


def render_action_preview(action_id: str, now: float | None = None) -> str:
    catalog = action_catalog()
    meta = catalog.get(action_id)
    cache = load_hud_cache()
    now = time.time() if now is None else now
    if action_id.startswith("_sep"):
        return _render_idle(cache, now, hint="section header")
    if meta is None:
        privileged = privilege_command()
        meta = {
            "title": action_id,
            "command": f"{privileged} pacman -S --needed {action_id}",
            "commands": [f"{privileged} pacman -S --needed {action_id}"],
            "blurb": "not in the main catalog · confirm still required",
            "mutates": True,
            "confirm": True,
        }
    command = meta["command"]
    mutates_payload = {"mutates": meta["mutates"]} if "mutates" in meta else {}
    mutates = bool(meta["mutates"]) if "mutates" in meta else False
    badge = render_status_badge(mutates_payload, "mutates")
    lines = [
        _prompt_line(command, mutates),
        f" {badge}",
        "",
        *[paint("38;5;189", line) for line in _wrap(str(meta.get("why") or meta["blurb"]), 48)],
        "",
    ]
    if meta["commands"]:
        lines.append(" " + paint(VIOLET, "commands"))
        for item in meta["commands"]:
            lines.append("   " + paint(DIM, "$") + " " + item)
        lines.append("")
    if not mutates:
        lines.extend(_ticker_block(cache, now))
    color = PINK if mutates else CYAN
    return _box(f"{meta['title']} · v{__version__}", lines, color=color) + "\n"


def _ticker_block(cache: dict[str, Any], now: float) -> list[str]:
    repo = cache.get("repo")
    aur = cache.get("aur")
    repo_s = "?" if repo is None else str(repo)
    aur_s = "?" if aur is None else str(aur)
    load = "?"
    load_path = "/proc/loadavg"
    if os.path.isfile(load_path):
        load = Path(load_path).read_text(encoding="utf-8").split()[0]
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    ticker = marquee(cache.get("ticker") or "open Status to list pending packages", width=46, now=now)
    return [
        " " + paint(DIM, f"{clock}  load {load}  repos {repo_s}  AUR {aur_s}"),
        " " + paint("38;5;245", ticker),
    ]


def _render_idle(cache: dict[str, Any], now: float, hint: str) -> str:
    lines = [
        " " + paint(DIM, hint),
        "",
        *_ticker_block(cache, now),
    ]
    return _box("preview", lines, color=DIM) + "\n"


_SI_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9 /-]{0,32}?)\s+:\s?(.*)$")


def parse_si(info: str) -> dict[str, str]:
    """Split pacman -Si. Key is the label before ' : ', so URL values keep their colons."""
    fields: dict[str, str] = {}
    current = ""
    for raw in info.splitlines():
        match = _SI_FIELD.match(raw)
        if match:
            current = match.group(1).strip()
            fields[current] = match.group(2).strip()
        elif current and raw.startswith((" ", "\t")):
            fields[current] = f"{fields.get(current, '')} {raw.strip()}".strip()
    return fields


def aur_hover_info(package: str) -> str:
    """Read-only AUR card for live search preview. Never shells out to yay."""
    cache = load_hud_cache()
    queued = ""
    for line in cache.get("aur_packages") or []:
        if line.split()[0] == package:
            queued = line
            break
    description = queued or "AUR · description from the queue, if the package is there"
    return (
        f"Name            : {package}\n"
        f"Version         :\n"
        f"Description     : {description}\n"
    )


REPO_TRUST_LABEL = "official catalog"
AUR_TRUST_LABEL = "community PKGBUILD · not a trust signal"
CACHE_TRUST_LABEL = "local cache · file from /var/cache/pacman/pkg, not a mirror"


def package_source_boundary(source: str) -> dict[str, str]:
    kind = (source or "").strip().lower()
    if not kind:
        return {
            "kind": UNKNOWN_SOURCE,
            "label": UNKNOWN_SOURCE,
            "trust": "no live source field",
            "color": MUTE,
            "command": "inspect only",
        }
    if kind == "aur":
        return {
            "kind": "aur",
            "label": "AUR",
            "trust": AUR_TRUST_LABEL,
            "color": PINK,
            "command": "yay -Si",
        }
    if kind == "cache":
        return {
            "kind": "cache",
            "label": "local cache",
            "trust": CACHE_TRUST_LABEL,
            "color": MUTE,
            "command": "pacman -Qi",
        }
    if kind == "repo":
        return {
            "kind": "repo",
            "label": "repo",
            "trust": REPO_TRUST_LABEL,
            "color": CYAN,
            "command": "pacman -Si",
        }
    return {
        "kind": kind,
        "label": kind,
        "trust": f"source {kind}",
        "color": MUTE,
        "command": "pacman -Si",
    }


def render_package_preview(source: str, package: str, info: str) -> str:
    source_payload: dict[str, Any] = {}
    if isinstance(source, str) and source.strip():
        source_payload["source"] = source.strip().lower()
    boundary = package_source_boundary(source_payload.get("source", ""))
    kind = boundary["kind"]
    color = boundary["color"]
    source_badge = render_status_badge(source_payload, "source")
    mutates_badge = render_status_badge({"mutates": False}, "mutates")
    if kind == UNKNOWN_SOURCE:
        command = "inspect only"
        prompt = " " + paint(MUTE, command)
    else:
        command = f"{boundary['command']} -- {package}"
        prompt = _prompt_line(command, mutates=False)
    fields = parse_si(info)
    lines = [
        prompt,
        " " + source_badge,
        " " + paint(MUTE, boundary["trust"]),
        " " + mutates_badge + paint(MUTE, "  ·  installed by Repos / AUR / Full"),
        "",
        " " + paint(f"1;{INK}", fields.get("Name") or package),
        " " + paint(MUTE, fields.get("Version") or ""),
    ]
    from .surface import parse_queue_versions

    versions = parse_queue_versions(info) or parse_queue_versions(fields.get("Description") or "")
    if versions:
        lines.append(" " + paint(MUTE, f"old {versions[0]} → new {versions[1]}"))
    description = fields.get("Description") or ""
    if description:
        lines.extend(paint(INK, line) for line in _wrap(description, 50)[:3])
    size = fields.get("Installed Size") or fields.get("Download Size") or ""
    if size:
        lines.append(" " + paint(MUTE, f"size  {size}"))
    depends = fields.get("Depends On") or ""
    if depends and depends != "None":
        short = depends if len(depends) <= 72 else depends[:71] + "…"
        lines.append(" " + paint(MUTE, f"depends  {short}"))
    if not fields:
        for raw in (info.strip() or "no Si").splitlines()[:12]:
            lines.append(" " + raw[:80])
    return _box(f"pkg · {kind} · {package}", lines, width=58, color=color) + "\n"


def render_chip_hud(state: dict[str, Any], last_run: dict[str, Any] | None, now: float | None = None) -> str:
    repo, aur = queue_counts_from_records(state)
    kernel = state.get("kernel") or {}
    running = kernel.get("running_package") or kernel.get("running_release") or "?"
    free = state.get("root", {}).get("free_bytes")
    free_s = f"{free / 1024**3:.1f} GiB" if isinstance(free, int) else "?"
    lock = "lock" if state.get("pacman_lock") else "no lock"
    pacnew = len(state.get("pacnew") or [])
    last = "—"
    if last_run:
        status = "FAIL" if last_run.get("failed") else "OK"
        last = f"{status} {last_run.get('mode')}"
    repo_s = UNKNOWN_SOURCE if repo is None else str(repo)
    aur_s = UNKNOWN_SOURCE if aur is None else str(aur)
    chips = "   ".join(
        [
            paint(f"1;{CYAN}", f"arch-update {__version__}"),
            paint(GREEN, free_s),
            paint(CYAN, f"{repo_s} repo"),
            paint(PINK, f"{aur_s} AUR"),
            paint("38;5;245", lock),
            paint(CYAN, f"{pacnew} pacnew"),
        ]
    )
    meta = paint("38;5;245", f"{running}  ·  last {last}")
    return f"{chips}\n{meta}"


def render_runway(snapshot: dict[str, Any], verification: dict[str, Any]) -> str:
    """Render the one read-only answer before opening a package transaction."""
    updates = snapshot.get("updates") or {}
    repo = updates.get("repo", "?")
    aur = updates.get("aur", "?")
    reboot_present = "reboot_required" in verification
    lock_present = "pacman_lock" in snapshot
    reboot = bool(verification["reboot_required"]) if reboot_present else False
    locked = bool(snapshot["pacman_lock"]) if lock_present else False
    attested = "healthy" in verification
    healthy = verification.get("healthy") if attested else None

    if reboot_present and reboot:
        headline = render_status_badge(verification, "reboot_required")
        detail = "new kernel already installed; the current session is still on the old module tree"
        action = "Attest"
        color = GOLD
    elif lock_present and locked:
        headline = render_status_badge(snapshot, "pacman_lock")
        detail = "pacman already holds the lock; Deck leaves it alone and does not start a second transaction"
        action = "Status"
        color = PINK
    elif healthy is True and reboot_present and lock_present:
        headline = (
            paint(f"1;{INK}", "READY TO REVIEW")
            + paint(DIM, " · reboot_required")
            + paint(DIM, " · pacman_lock")
            + paint(DIM, " · healthy")
        )
        detail = "overturns if pacman_lock or reboot_required · Attest healthy, not an install permit"
        action = "Plan"
        color = GREEN
    else:
        bits: list[str] = []
        if not attested:
            bits.append("unattested")
        elif healthy is False:
            bits.append("healthy=false")
        if not reboot_present or not lock_present:
            missing = "reboot_required" if not reboot_present else "pacman_lock"
            bits.append(f"{UNKNOWN_SOURCE} · {missing}")
        headline = " · ".join(bits) if bits else "unattested"
        detail = "no attestation or live reboot_required/pacman_lock fields; no readiness verdict is given"
        action = "Attest"
        color = MUTE

    root = snapshot.get("root") or {}
    free = root.get("free_bytes")
    space = f"{free / 1024**3:.1f} GiB root free" if isinstance(free, int) else "root free unknown"
    if isinstance(free, int) and free < HARD_FREE_BYTES:
        space += " · below hard floor"
    lines = [
        " " + headline,
        " " + paint(MUTE, detail),
        "",
        " " + paint(CYAN, f"{repo} repo") + paint(MUTE, " · ") + paint(PINK, f"{aur} AUR"),
        " " + paint(MUTE, space),
        "",
        " " + paint(MUTE, "next") + " " + paint(f"1;{CYAN}", action),
        " " + paint(MUTE, "source local · no mutation"),
    ]
    return _box("runway", lines, width=74, color=color) + "\n"


def render_attest_board(result: dict[str, Any]) -> str:
    issues = result.get("issues") or []
    inventory = result.get("inventory") or []
    pending = result.get("pending") or {}
    blocking = [issue for issue in issues if issue.get("mode") != "PACNEW-PENDING"]
    reboot = bool(result.get("reboot_required"))
    attested = "healthy" in result
    healthy = bool(result["healthy"]) if attested else False
    by_mode = {issue["mode"]: issue for issue in blocking}

    def row(label: str, ok: bool, detail: str, warn: bool = False) -> str:
        return f" {_lamp(ok, warn=warn)}  {paint('1;38;5;189', f'{label:<10}')} {paint('38;5;245', detail)}"

    if attested:
        health_detail = "clear" if healthy else f"{len(blocking)} blocking"
        health_ok = healthy
    else:
        health_detail = "unknown"
        health_ok = False

    lines = [
        " " + paint("1;38;5;213", "* Attest") + "  " + paint("38;5;245", "health after updates · not a log"),
        "",
        row("health", health_ok, health_detail),
        row("reboot", not reboot, "not required" if not reboot else "required — this tool will not reboot", warn=reboot),
        row(
            "pending",
            True,
            f"repos {pending.get('repo', '?')} · AUR {pending.get('aur', '?')}  (queue, not an emergency)",
        ),
        row("pacnew", not inventory, "none" if not inventory else f"{len(inventory)} new defaults next to yours"),
    ]
    if "RUNNING-KERNEL-MODULES-MISSING" in by_mode:
        lines.append(row("modules", False, by_mode["RUNNING-KERNEL-MODULES-MISSING"]["message"]))
    else:
        lines.append(row("modules", True, "running tree present"))
    if "HYPRLAND-ABI-FORK" in by_mode:
        lines.append(row("hypr", False, by_mode["HYPRLAND-ABI-FORK"]["message"][:60]))
    else:
        lines.append(row("hypr", True, "ABI linked"))
    for issue in blocking:
        if issue["mode"] in {"RUNNING-KERNEL-MODULES-MISSING", "HYPRLAND-ABI-FORK"}:
            continue
        lines.append(row(issue["mode"][:10].lower(), False, issue["message"][:60]))
    if inventory:
        lines.append("")
        lines.append(" " + paint(DIM, "pacnew"))
        for item in inventory[:8]:
            lines.append("   " + paint("38;5;245", item.get("message", "")))
        if len(inventory) > 8:
            lines.append("   " + paint(DIM, f"+{len(inventory) - 8} more"))
    return _box("attest", lines, width=62, color=PINK if not healthy else CYAN) + "\n"


def render_help() -> str:
    lines = [
        " " + paint(f"1;{CYAN}", "Can do"),
        " " + paint(MUTE, "Package queue, search (zen → kernel), repo/AUR updates,"),
        " " + paint(MUTE, "kernel+GRUB, update health, pacnew, orphans, pacman.log."),
        " " + paint(MUTE, "Updater Engine · Plan · Runway · Schedule"),
        " " + paint(MUTE, "source local · no mutation"),
        "",
    ]
    for item_id, title, subtitle in ACTION_SLOTS:
        if item_id.startswith("_sep") or item_id == "help":
            continue
        lines.append(" " + paint(f"1;{INK}", title) + paint(MUTE, f"  {subtitle}"))
        for wrapped in _wrap(WHY.get(item_id, ""), 50):
            lines.append(paint("38;5;240", wrapped))
        lines.append("")
    return _box("help", lines, width=62, color=CYAN) + "\n"


def render_orphans() -> str:
    result = run_capture(["pacman", "-Qtd"], timeout=30)
    lines = [" " + paint(f"1;{CYAN}", "Orphans  pacman -Qtd"), ""]
    body = [line for line in result.output.splitlines() if line.strip()]
    if result.returncode != 0 or not body:
        lines.append(" " + paint(MUTE, "empty list (or pacman did not answer)"))
    else:
        lines.append(" " + paint(MUTE, f"{len(body)} packages nothing depends on"))
        lines.append("")
        lines.extend(" " + line for line in body[:80])
    return _box("orphans", lines, width=62, color=CYAN) + "\n"


def fetch_arch_news(timeout: float = 3.0) -> list[str]:
    import urllib.request

    request = urllib.request.Request(
        "https://archlinux.org/feeds/news/",
        headers={"User-Agent": "arch-update-deck"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(80_000).decode("utf-8", "replace")
    titles: list[str] = []
    for block in raw.split("<item>")[1:]:
        match = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        if not match:
            continue
        title = re.sub(r"<[^>]+>", "", match.group(1))
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
        if title:
            titles.append(title)
        if len(titles) >= 12:
            break
    return titles


def render_arch_news() -> str:
    lines = [" " + paint(f"1;{CYAN}", "Arch Linux News"), ""]
    try:
        titles = fetch_arch_news()
    except Exception as exc:
        lines.append(" " + paint(MUTE, f"RSS unavailable ({exc.__class__.__name__})"))
        return _box("news", lines, width=62, color=CYAN) + "\n"
    if not titles:
        lines.append(" " + paint(MUTE, "empty feed"))
    else:
        lines.extend(" " + title for title in titles)
    return _box("news", lines, width=62, color=CYAN) + "\n"


def render_pacman_log(limit: int = 40) -> str:
    path = Path("/var/log/pacman.log")
    lines = [" " + paint(f"1;{CYAN}", "pacman.log"), ""]
    if not path.is_file():
        lines.append(" " + paint(MUTE, "no /var/log/pacman.log"))
        return _box("log", lines, width=72, color=CYAN) + "\n"
    keep = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "[ALPM] upgraded " in raw or "[ALPM] installed " in raw or "[ALPM] removed " in raw:
            keep.append(raw)
    tail = keep[-limit:]
    if not tail:
        lines.append(" " + paint(MUTE, "no upgraded/installed/removed"))
    else:
        lines.extend(" " + line for line in tail)
    return _box("log", lines, width=72, color=CYAN) + "\n"
