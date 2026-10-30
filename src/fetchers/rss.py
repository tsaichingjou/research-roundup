"""Generic RSS / Atom fetcher.

Download is done with `requests` (so we control timeouts, retries and the
User-Agent); parsing is delegated to `feedparser`, which copes with RSS 0.9x,
RSS 1.0 (RDF), RSS 2.0 and Atom.
"""

from __future__ import annotations

import html
import logging
import re
from calendar import timegm
from datetime import datetime, timezone
from typing import Optional

import feedparser

from src.fetchers.http import FetchError, download
from src.models import Paper

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
# Some feeds (e.g. NBER) append the authors to the title: "Title -- by A, B".
_TITLE_AUTHORS_RE = re.compile(r"^(?P<title>.+?)\s+-{1,2}\s+by\s+(?P<authors>.+)$", re.IGNORECASE)


def split_authors(text: str) -> list[str]:
    parts = re.split(r",\s*|\s+and\s+|\s*&\s*", text)
    return [p.strip() for p in parts if p.strip()]


def clean_text(raw: str) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    text = _TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            return datetime.fromtimestamp(timegm(struct), tz=timezone.utc)
    return None


def _parse_authors(entry) -> list[str]:
    if entry.get("authors"):
        names = [a.get("name", "").strip() for a in entry["authors"]]
        names = [n for n in names if n]
        # Some feeds put every author in a single comma-separated string.
        if len(names) == 1 and ("," in names[0] or " and " in names[0]):
            names = [n.strip() for n in re.split(r",| and ", names[0]) if n.strip()]
        return names
    if entry.get("author"):
        return [n.strip() for n in re.split(r",| and ", entry["author"]) if n.strip()]
    return []


def parse_feed(content: bytes, source: dict) -> list[Paper]:
    """Turn raw feed bytes into Paper objects. Kept separate from the
    network call so it can be unit-tested with saved fixtures."""
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise FetchError(f"{source['id']}: could not parse feed ({feed.bozo_exception})")

    papers = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "").strip()
        if not title or not link:
            continue  # unusable entry
        abstract = clean_text(entry.get("summary") or entry.get("description") or "")
        authors = _parse_authors(entry)
        m = _TITLE_AUTHORS_RE.match(title)
        if m:
            title = m.group("title").strip()
            authors = authors or split_authors(m.group("authors"))
        papers.append(
            Paper(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                url=link,
                abstract=abstract,
                authors=authors,
                published=_parse_date(entry),
            )
        )
    return papers


def fetch(source: dict, settings: dict) -> list[Paper]:
    content = download(source["url"], settings)
    return parse_feed(content, source)
