"""Recover missing abstracts so more papers can be assessed.

Some channels deliver a title but no abstract (Crossref records for JPE, some
BIS and Fed in Print items). Before such a paper is written off as "not
assessed", two open sources are tried in order:

  1. OpenAlex (https://openalex.org) — a free scholarly index that stores
     abstracts for most journal articles and working papers, looked up by DOI,
     or by title when there is no DOI (accepted only on a near-exact title match).
  2. The paper's own landing page — publishers and central banks expose the
     abstract in standard metadata tags (citation_abstract, dc.description,
     og:description) even when the full text is paywalled.
  3. The opening of the paper's open text (src/fulltext.py) — for speeches,
     blog posts and working papers whose page has no abstract tag: the text
     after an "Abstract" heading in the PDF, or else the first paragraphs.

The source of every recovered abstract is stored (`abstract_source`) and shown
in the explorer, and a paper is not retried for a week after a failed attempt.
Only openly published pages and documents are read; no paywall or bot check is
bypassed, so paywalled journals without an open abstract stay excluded.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser

from src.fetchers.http import FetchError, QuotaExceeded, download
from src.fetchers.openalex import reconstruct_abstract  # noqa: F401 (re-exported)
from src.fetchers.rss import clean_text
from src.fulltext import FullTextFinder, opening
from src.models import normalise_title
from src.storage import Storage

log = logging.getLogger(__name__)

OPENALEX = "https://api.openalex.org"
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)
_META_KEYS = ("citation_abstract", "dc.description", "dcterms.abstract", "description",
              "og:description", "twitter:description")
_GENERIC = re.compile(r"cookie|javascript|subscribe|sign in|log in|this website|page not found", re.I)


def doi_of(url: str) -> str | None:
    m = _DOI_RE.search(url or "")
    return m.group(1).rstrip(".").lower() if m else None


def titles_match(a: str, b: str) -> bool:
    a, b = normalise_title(a), normalise_title(b)
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.92


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("name") or a.get("property") or "").lower()
        if key in _META_KEYS and a.get("content"):
            self.meta.setdefault(key, []).append(a["content"])


def abstract_from_html(html: str, min_chars: int) -> str | None:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # malformed pages: take what was parsed
        pass
    for key in _META_KEYS:  # most specific tag first
        for value in parser.meta.get(key, []):
            text = re.sub(r"^\s*abstract[:.\s]*", "", clean_text(value), flags=re.I)
            if len(text) >= min_chars and not _GENERIC.search(text[:200]):
                return text
    return None


class Enricher:
    def __init__(self, settings: dict, min_chars: int = 200, use_openalex: bool = True,
                 use_landing_page: bool = True, max_title_searches: int = 40,
                 use_page_text: bool = True, opening_chars: int = 1500):
        # Lookups are best-effort: one quick attempt each, no long retry loops.
        self.settings = {**settings, "max_retries": 1, "request_timeout_seconds": 15}
        self.min_chars = min_chars
        self.use_openalex = use_openalex
        self.use_landing_page = use_landing_page
        self.mailto = settings.get("contact_email")
        # DOI lookups are free on OpenAlex; title searches are billed, so they are capped.
        self.title_searches_left = max_title_searches
        self.quota_note = None
        self.api_key = os.environ.get("OPENALEX_API_KEY")
        self.use_page_text = use_page_text
        self.opening_chars = opening_chars
        # Short documents (a two-page speech) still count: a lower bar than for write-ups.
        self.finder = FullTextFinder(self.settings, max_chars=20000, min_chars=max(min_chars, 600))

    def _openalex_params(self, **extra) -> dict:
        params = {"select": "title,abstract_inverted_index", **extra}
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def from_openalex(self, paper: dict) -> str | None:
        doi = doi_of(paper["url"])
        if doi:
            data = json.loads(download(f"{OPENALEX}/works/doi:{doi}", self.settings,
                                       params=self._openalex_params()))
            candidates = [data]
        else:
            if self.title_searches_left <= 0:
                return None
            self.title_searches_left -= 1
            data = json.loads(download(f"{OPENALEX}/works", self.settings,
                                       params=self._openalex_params(search=paper["title"][:200], per_page=3)))
            candidates = data.get("results", [])
        for work in candidates:
            if work.get("title") and titles_match(work["title"], paper["title"]):
                text = clean_text(reconstruct_abstract(work.get("abstract_inverted_index")))
                if len(text) >= self.min_chars:
                    return text
        return None

    def from_landing_page(self, paper: dict) -> str | None:
        html = download(paper["url"], self.settings).decode("utf-8", errors="replace")
        return abstract_from_html(html, self.min_chars)

    def from_page_text(self, paper: dict) -> str | None:
        found = self.finder.find(paper)
        if not found:
            return None
        text = opening(found.text, self.opening_chars)
        return text if len(text) >= self.min_chars else None

    def enrich(self, paper: dict) -> tuple[str, str] | None:
        steps = []
        if self.use_openalex:
            steps.append(("OpenAlex", self.from_openalex))
        if self.use_landing_page and not paper["url"].lower().endswith(".pdf"):
            steps.append(("publisher page", self.from_landing_page))
        if self.use_page_text:
            steps.append(("opening of the text", self.from_page_text))
        for name, step in steps:
            if name == "OpenAlex" and not self.use_openalex:
                continue
            try:
                text = step(paper)
            except QuotaExceeded as exc:
                self.use_openalex = False          # stop calling OpenAlex for the rest of this run
                self.quota_note = str(exc)
                log.warning("OpenAlex: %s; continuing with publisher pages only", exc)
                continue
            except (FetchError, ValueError, KeyError) as exc:
                log.debug("%s lookup failed for %s: %s", name, paper["uid"], exc)
                continue
            if text:
                return text, name
        return None


def run_enrichment(storage: Storage, settings: dict, cfg: dict, retry_all: bool = False) -> dict:
    """Try to recover abstracts for papers that lack one. Returns a summary."""
    ecfg = cfg.get("enrichment", {})
    if not ecfg.get("enabled", True):
        return {"tried": 0, "found": Counter(), "by_source": {}}
    min_chars = cfg["prefilter"]["min_abstract_chars"]
    retry_before = (datetime.now(timezone.utc) - timedelta(days=0 if retry_all else ecfg.get("retry_after_days", 7))).isoformat()
    todo = storage.papers_missing_abstract(min_chars, retry_before)[: ecfg.get("max_per_run", 300)]
    enricher = Enricher(settings, min_chars, ecfg.get("openalex", True), ecfg.get("landing_page", True),
                        ecfg.get("max_title_searches", 40), ecfg.get("page_text", True),
                        cfg["screening"].get("max_abstract_chars", 1500))
    found, by_source = Counter(), {}
    for i, paper in enumerate(todo, 1):
        result = enricher.enrich(paper)
        stats = by_source.setdefault(paper["source_name"], Counter())
        stats["tried"] += 1
        if result:
            text, origin = result
            storage.set_abstract(paper["uid"], text, origin)
            found[origin] += 1
            stats["found"] += 1
        else:
            storage.mark_enrich_tried(paper["uid"])
        if i % 20 == 0:
            log.info("Abstract lookup: %d/%d papers tried, %d recovered", i, len(todo), sum(found.values()))
    return {"tried": len(todo), "found": found, "by_source": by_source, "note": enricher.quota_note}
