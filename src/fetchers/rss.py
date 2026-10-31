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
from urllib.parse import urlparse

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


class InstitutionResolver:
    """Attributes items of an aggregated feed to the institution that issued them,
    from the link's domain or, failing that, a RePEc archive code in the link."""

    def __init__(self, institutions: list[dict]):
        self.by_domain, self.by_repec = {}, {}
        for inst in institutions:
            for d in inst.get("domains", []):
                self.by_domain[d.lower()] = inst
            for code in inst.get("repec", []):
                self.by_repec[code.lower()] = inst

    def resolve(self, link: str) -> Optional[dict]:
        host = (urlparse(link).hostname or "").lower()
        parts = host.split(".")
        for i in range(len(parts) - 1):          # www.ecb.europa.eu -> ecb.europa.eu -> europa.eu
            inst = self.by_domain.get(".".join(parts[i:]))
            if inst:
                return inst
        m = re.search(r"repec[^/]*/[a-z]/([a-z0-9]+)/|RePEc:([a-z0-9]+):", link, re.IGNORECASE)
        if m:
            return self.by_repec.get((m.group(1) or m.group(2)).lower())
        return None


def parse_feed(content: bytes, source: dict) -> list[Paper]:
    """Turn raw feed bytes into Paper objects. Kept separate from the
    network call so it can be unit-tested with saved fixtures."""
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise FetchError(f"{source['id']}: could not parse feed ({feed.bozo_exception})")

    resolver = InstitutionResolver(source["_institutions"]) if source.get("_institutions") else None
    papers = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "").strip()
        if not title or not link:
            continue  # unusable entry
        source_id, source_name = source["id"], source["name"]
        if resolver:
            inst = resolver.resolve(link)
            if inst and inst.get("skip"):
                continue  # covered by a dedicated source
            if inst:
                source_id, source_name = inst["id"], inst["name"]
            else:
                host = (urlparse(link).hostname or "unknown").removeprefix("www.")
                log.info("[%s] no institution mapped for %s", source["id"], host)
                source_id, source_name = f"other:{host}", f"Other central bank ({host})"
        abstract = clean_text(entry.get("summary") or entry.get("description") or "")
        authors = _parse_authors(entry)
        m = _TITLE_AUTHORS_RE.match(title)
        if m:
            title = m.group("title").strip()
            authors = authors or split_authors(m.group("authors"))
        papers.append(
            Paper(
                source_id=source_id,
                source_name=source_name,
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
