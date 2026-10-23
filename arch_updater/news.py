"""Arch Linux news that asks for a manual step before an upgrade.

An unattended `pacman -Syu --noconfirm` cannot do what such a post says, so the
weekly run stops until a person has read it. The feed is read, never acted on.
A feed that cannot be fetched or parsed does not stop anything: a dead news
server must not hold back security updates.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import atomic
from .shell import Shell

FEED = "https://archlinux.org/feeds/news/"
SEEN = "news-seen.json"
CACHE = "news-feed.xml"  # the last feed that parsed: a run looks several times, in several processes
CACHE_SECONDS = 1800
FETCH_TIMEOUT = 5  # a slow news server is a dead one; the look waits for every check
WINDOW_DAYS = 30  # a post older than this no longer holds a run back
MANUAL = re.compile(
    r"manual\s+(?:intervention|action|step)|requires?\s+(?:user\s+)?intervention|action\s+required|before\s+(?:upgrading|updating)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    link: str
    published: float


def parse(document: str) -> list[Item]:
    """The posts of an RSS feed. Anything that is not one raises ValueError."""
    try:
        channel = ET.fromstring(document).find("channel")
    except ET.ParseError as exc:
        raise ValueError(f"news feed is not XML: {exc}") from exc
    if channel is None:
        raise ValueError("news feed has no channel")
    items = []
    for node in channel.findall("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        body = node.findtext("description") or ""
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            continue  # a post with no date cannot be told old from new: it holds nothing back
        if MANUAL.search(f"{title} {body}"):
            items.append(Item(node.findtext("guid") or link or title, title, link, published))
    return items


def seen(state: Path) -> set[str] | None:
    """The posts a person has read, or None when this machine never looked (the first look reads them all as read)."""
    try:
        return set(json.loads((state / SEEN).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def acknowledge(items: list[Item], state: Path) -> None:
    atomic.write(state / SEEN, json.dumps(sorted((seen(state) or set()) | {item.id for item in items})))


def posts(sh: Shell, state: Path, now: float) -> list[Item]:
    """The manual-step posts of the feed, from the cache while it is fresh. Raises OSError or ValueError when it cannot be read."""
    cached = state / CACHE
    try:
        if now - cached.stat().st_mtime < CACHE_SECONDS:
            return parse(cached.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass  # none, too old or damaged: ask again
    document = sh.get(FEED, timeout=FETCH_TIMEOUT)
    found = parse(document)
    atomic.write(cached, document)
    return found


def waiting(sh: Shell, state: Path, now: float | None = None) -> list[Item]:
    """Manual-step posts from the last WINDOW_DAYS that nobody has acknowledged. Raises OSError or ValueError when the feed cannot be read."""
    now = time.time() if now is None else now
    items = posts(sh, state, now)
    read = seen(state)
    if read is None:  # first look: whatever is there was there before this tool watched
        acknowledge(items, state)
        return []
    return [item for item in items if item.id not in read and item.published >= now - WINDOW_DAYS * 86400]
