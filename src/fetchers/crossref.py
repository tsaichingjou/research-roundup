"""Crossref REST API fetcher.

Crossref is the DOI registration agency used by almost every academic
publisher. Querying it by ISSN returns a journal's latest articles as clean
JSON, which is far more stable than scraping publisher websites (several of
which block bots). Used for the top-5 journals and IMF Working Papers.

API docs: https://api.crossref.org/swagger-ui/index.html
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.fetchers.http import FetchError, download
from src.fetchers.rss import clean_text
from src.models import Paper

log = logging.getLogger(__name__)

API = "https://api.crossref.org/journals/{issn}/works"

# Editorial items that are not research papers.
_DEFAULT_EXCLUDE = [
    r"^front matter", r"^back matter", r"^volume information", r"^issue information",
    r"^table of contents", r"^editorial", r"^erratum", r"^corrigendum", r"^correction",
    r"^announcement", r"^report of the", r"^minutes of", r"^index", r"^masthead",
    r"^recent referees", r"^forthcoming papers", r"^notice", r"^frontmatter", r"^backmatter",
    r"^submission of manuscripts", r"^instructions (for|to) authors", r"^call for papers",
]


def _parse_date(item: dict) -> Optional[datetime]:
    """Best available publication date.

    Some publishers (e.g. Econometrica) only give a year ([[2026]]). A year
    alone would be read as 1 January, so a paper out this month would look ten
    months old. We therefore take the first date with at least month
    precision, then fall back to `created` (when the DOI was registered, always
    a full timestamp), and only use a year-only date as a last resort.
    """
    year_only = None
    for key in ("published-online", "published-print", "published", "issued", "created"):
        parts = (item.get(key) or {}).get("date-parts")
        if not (parts and parts[0] and parts[0][0]):
            continue
        p = parts[0]
        if len(p) == 1:
            year_only = year_only or p
            continue
        y, m, d = (p + [1])[:3]
        try:
            return datetime(int(y), int(m), int(d or 1), tzinfo=timezone.utc)
        except ValueError:
            continue
    if year_only:
        return datetime(int(year_only[0]), 1, 1, tzinfo=timezone.utc)
    return None


def _authors(item: dict) -> list[str]:
    names = []
    for a in item.get("author", []):
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
        if name:
            names.append(name.strip())
    return names


def parse_response(content: bytes, source: dict) -> list[Paper]:
    try:
        items = json.loads(content)["message"]["items"]
    except (ValueError, KeyError) as exc:
        raise FetchError(f"{source['id']}: unexpected Crossref response ({exc})")

    exclude = [re.compile(p, re.IGNORECASE) for p in _DEFAULT_EXCLUDE + source.get("exclude_titles", [])]
    papers = []
    for item in items:
        title = clean_text(" ".join(item.get("title") or []))
        if not title or any(p.search(title) for p in exclude):
            continue
        doi = item.get("DOI", "")
        url = f"https://doi.org/{doi}" if doi else item.get("URL", "")
        if not url:
            continue
        papers.append(
            Paper(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                url=url,
                abstract=clean_text(item.get("abstract", "")),  # JATS XML -> plain text
                authors=_authors(item),
                published=_parse_date(item),
            )
        )
    return papers


def fetch(source: dict, settings: dict) -> list[Paper]:
    lookback = source.get("lookback_days", settings.get("lookback_days", 30))
    since = (datetime.now(timezone.utc) - timedelta(days=lookback)).date().isoformat()
    # Filter on `created` (DOI registration date, always precise) rather than the
    # publication date, which some journals record with year-only precision and
    # would therefore be silently excluded by a "last 30 days" filter.
    params = {
        "filter": f"from-created-date:{since}",
        "sort": "created",
        "order": "desc",
        "rows": source.get("max_items", 100),
        "select": "DOI,URL,title,author,abstract,published,published-online,published-print,issued,created,type",
    }
    if settings.get("contact_email"):
        params["mailto"] = settings["contact_email"]  # Crossref "polite pool": faster, more reliable

    content = download(API.format(issn=source["issn"]), settings, params=params)
    return parse_response(content, source)
