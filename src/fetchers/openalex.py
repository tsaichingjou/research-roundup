"""OpenAlex fetcher: recent works by institution (central banks) or by journal ISSN.

Why OpenAlex for central banks?
  Each central bank publishes on its own differently built website, and the
  BIS Central Bank Research Hub feed turned out to carry only a handful of
  items and to lag by months. OpenAlex (https://openalex.org), a free index of
  scholarly works, can be queried by *institution*: "works by Bank of Japan
  authors published in the last 60 days". One uniform API covers every bank in
  config/institutions.yaml, and most records include the abstract.

Modes (set in sources.yaml)
  institutions: true   one query per institution in config/institutions.yaml
                       (groups chosen with `include_groups`); each paper is
                       credited to that institution.
  issn: [print, online]  works published in one journal or series (used for BPEA).

Institution names are resolved to OpenAlex IDs with the /institutions search
and cached in data/openalex_ids.json; a wrong match can be pinned by adding
`openalex_id: I...` to the institution in config/institutions.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.fetchers.http import FetchError, QuotaExceeded, download
from src.fetchers.rss import clean_text
from src.models import Paper

log = logging.getLogger(__name__)

API = "https://api.openalex.org"
ROOT = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / "data" / "openalex_ids.json"
SELECT = "id,doi,display_name,publication_date,authorships,abstract_inverted_index,primary_location"


def reconstruct_abstract(inverted: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not inverted:
        return ""
    words = sorted((pos, w) for w, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words)


def _params(settings: dict, **extra) -> dict:
    p = dict(extra)
    if settings.get("contact_email"):
        p["mailto"] = settings["contact_email"]
    if os.environ.get("OPENALEX_API_KEY"):
        p["api_key"] = os.environ["OPENALEX_API_KEY"]
    return p


_last_call = [0.0]


def _get(url: str, settings: dict, **params) -> dict:
    """GET with polite pacing: OpenAlex rate-limits bursts of requests."""
    gap = settings.get("openalex_min_interval", 0.5)
    wait = gap - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    return json.loads(download(url, settings, params=_params(settings, **params)))


def name_key(name: str) -> str:
    """Accent-insensitive key for institution names: 'Österreichische' -> 'osterreichische'.
    Returns '' for names with no Latin letters, which then never match anything."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def resolve_institution(inst: dict, settings: dict, cache: dict) -> tuple[str, str] | None:
    """Return (openalex_id, display_name) for an institution, or None."""
    if inst.get("openalex_id"):
        return inst["openalex_id"], inst["name"]
    if inst["id"] in cache:
        return tuple(cache[inst["id"]]) if cache[inst["id"]] else None
    names = [inst.get("openalex_name") or inst["name"]] + inst.get("aliases", [])
    found = None
    for name in names:
        results = _get(f"{API}/institutions", settings, search=name, per_page=5,
                       select="id,display_name,display_name_alternatives").get("results", [])
        for r in results:
            candidates = [r.get("display_name", "")] + (r.get("display_name_alternatives") or [])
            # exact match on the normalised name: "Bank of Canada" must not match "Bank of Ghana"
            if any(name_key(c) and name_key(c) == name_key(n) for c in candidates for n in names):
                found = (r["id"].rsplit("/", 1)[-1], r["display_name"])
                break
        if found:
            break
    cache[inst["id"]] = list(found) if found else None
    _save_cache(cache)          # keep progress even if the run is interrupted
    return found


def _to_paper(work: dict, source_id: str, source_name: str) -> Paper | None:
    title = clean_text(work.get("display_name") or "")
    abstract = clean_text(reconstruct_abstract(work.get("abstract_inverted_index")))
    loc = work.get("primary_location") or {}
    url = work.get("doi") or loc.get("landing_page_url") or work.get("id")
    if not title or not url:
        return None
    published = None
    if work.get("publication_date"):
        published = datetime.fromisoformat(work["publication_date"]).replace(tzinfo=timezone.utc)
    authors = [a["author"]["display_name"] for a in work.get("authorships", [])
               if a.get("author", {}).get("display_name")]
    return Paper(source_id=source_id, source_name=source_name, title=title, url=url,
                 abstract=abstract, authors=authors, published=published)


def works(filters: list[str], settings: dict, per_page: int = 50) -> list[dict]:
    return _get(f"{API}/works", settings, filter=",".join(filters), sort="publication_date:desc",
                per_page=per_page, select=SELECT).get("results", [])


def fetch(source: dict, settings: dict) -> list[Paper]:
    lookback = source.get("lookback_days", 60)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback)).date().isoformat()
    base = [f"from_publication_date:{since}"]
    if source.get("require_abstract", True):
        base.append("has_abstract:true")

    if source.get("issn"):
        # a journal can be indexed under its print or its online ISSN: match either, in any location
        issns = source["issn"] if isinstance(source["issn"], list) else [source["issn"]]
        return [p for w in works(base + [f"locations.source.issn:{'|'.join(issns)}"], settings)
                if (p := _to_paper(w, source["id"], source["name"]))]

    # institution mode
    groups = set(source.get("include_groups") or [])
    insts = [i for i in source.get("_institutions", [])
             if not i.get("skip") and (not groups or i.get("group") in groups)]
    cache, papers, diagnostics, failures = _load_cache(), [], {}, 0
    for inst in insts:
        try:
            resolved = resolve_institution(inst, settings, cache)
            if not resolved:
                diagnostics[inst["name"]] = "not found in OpenAlex (add openalex_id or openalex_name)"
                continue
            oa_id, oa_name = resolved
            found = [p for w in works(base + [f"institutions.id:{oa_id}"], settings, per_page=source.get("per_institution", 25))
                     if (p := _to_paper(w, inst["id"], inst["name"]))]
            papers += found
            diagnostics[inst["name"]] = f"{len(found)} works ({oa_id}, matched '{oa_name}')"
        except QuotaExceeded as exc:
            # Budget gone: skip the remaining banks instead of failing each one slowly.
            log.warning("[%s] %s; skipping the remaining institutions", source["id"], exc)
            for rest in insts[insts.index(inst):]:
                diagnostics.setdefault(rest["name"], f"skipped: {exc}")
            if not papers:
                _save_cache(cache)
                source["_diagnostics"] = diagnostics
                raise
            break
        except (FetchError, ValueError, KeyError) as exc:
            failures += 1
            diagnostics[inst["name"]] = f"error: {str(exc)[:80]}"
            log.warning("[%s] %s: %s", source["id"], inst["name"], exc)
    _save_cache(cache)
    source["_diagnostics"] = diagnostics
    if insts and failures == len(insts):
        raise FetchError(f"all {failures} institution queries failed")
    return papers
