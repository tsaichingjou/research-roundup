"""Analysis stage: pre-filter -> screen (LLM, batched) -> select -> deep analysis (LLM).

Why two LLM stages?
  A month brings 150-300 new papers. Writing a full analysis for each would be
  slow and wasteful on a rate-limited free tier. Instead every paper gets a
  cheap relevance score in batches of ~12, and only the 5-8 selected papers get
  the full write-up. Results are cached in SQLite, so re-running never pays for
  the same paper twice.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src import prompts, quality
from src.fulltext import DATASET_HOSTS  # replication packages, not papers
from src.llm import JSONModel, LLMAuthError, LLMError
from src.storage import Storage

log = logging.getLogger(__name__)

MARKET_LABELS = {"em": "EM", "dm": "DM", "transversal": "Transversal"}


def load_analysis_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalise_market(value) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("emerging markets", "em").replace("developed markets", "dm")
    return MARKET_LABELS.get(key)


# --- 1. Pre-filter --------------------------------------------------------------
def prefilter(papers: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Drop papers that cannot be analysed. Returns (kept, excluded)."""
    min_chars = cfg["prefilter"]["min_abstract_chars"]
    patterns = [re.compile(p, re.IGNORECASE) for p in cfg["prefilter"]["exclude_title_patterns"]]
    kept, excluded = [], []
    for p in papers:
        if any(h in (p.get("url") or "") for h in DATASET_HOSTS):
            excluded.append({"uid": p["uid"], "status": "excluded", "reason": "dataset, not a paper"})
        elif any(rx.search(p["title"]) for rx in patterns):
            excluded.append({"uid": p["uid"], "status": "excluded", "reason": "not a research paper"})
        elif len(p.get("abstract") or "") < min_chars:
            excluded.append({"uid": p["uid"], "status": "excluded", "reason": "no abstract available"})
        else:
            kept.append(p)
    return kept, excluded


# --- 2. Screening -----------------------------------------------------------------
def _validate_screening(item: dict) -> dict | None:
    try:
        score = max(0, min(10, int(round(float(item["score"])))))
    except (KeyError, TypeError, ValueError):
        return None
    market = normalise_market(item.get("market_focus"))
    if market is None:
        return None
    return {
        "score": score,
        "market_focus": market,
        "topic": str(item.get("topic", "")).strip()[:60],
        "reason": str(item.get("reason", "")).strip()[:400],
    }


def screen(papers: list[dict], client: JSONModel, cfg: dict, on_batch=None) -> list[dict]:
    """Score papers in batches. Papers the model skips are left unscreened and
    retried on the next run rather than guessed. `on_batch(results)` is called
    after every batch so progress is saved even if the run is interrupted."""
    batch_size = cfg["screening"]["batch_size"]
    max_chars = cfg["screening"]["max_abstract_chars"]
    results: list[dict] = []

    for start in range(0, len(papers), batch_size):
        batch = papers[start:start + batch_size]
        # Short local ids (p1, p2...) are more reliable for the model than 16-char hashes.
        id_map = {f"p{i + 1}": p["uid"] for i, p in enumerate(batch)}
        prompt = prompts.screening_prompt([
            {"id": short, "source": p["source_name"], "title": p["title"],
             "abstract": p["abstract"][:max_chars]}
            for short, p in zip(id_map, batch)
        ])
        try:
            response = client.generate_json(prompt, system=prompts.SYSTEM)
        except LLMAuthError:
            raise  # a bad key fails every batch: stop at once
        except LLMError as exc:
            log.error("Screening batch %d failed: %s", start // batch_size + 1, exc)
            continue
        if isinstance(response, dict):  # some models wrap the array
            response = next((v for v in response.values() if isinstance(v, list)), [])

        seen, batch_results = set(), []
        for item in response if isinstance(response, list) else []:
            uid = id_map.get(str(item.get("id", "")).strip())
            valid = _validate_screening(item) if uid else None
            if valid and uid not in seen:
                seen.add(uid)
                batch_results.append({"uid": uid, "status": "scored", **valid})
        results += batch_results
        if on_batch:
            on_batch(batch_results)
        missing = len(batch) - len(seen)
        if missing:
            log.warning("Screening batch %d: %d paper(s) not returned; will retry next run",
                        start // batch_size + 1, missing)
        log.info("Screened %d/%d papers", min(start + batch_size, len(papers)), len(papers))
    return results


def classify_titles(papers: list[dict], client: JSONModel, cfg: dict) -> list[dict]:
    """Market-focus label from the title alone, for papers with no abstract.

    They get status "title_only" and no score: the label lets the dashboard show
    every publication by market focus, but a title is too little to rank on, so
    these papers are never selected for the newsletter."""
    batch_size = cfg["screening"].get("title_batch_size", 20)
    results: list[dict] = []
    for start in range(0, len(papers), batch_size):
        batch = papers[start:start + batch_size]
        id_map = {f"p{i + 1}": p["uid"] for i, p in enumerate(batch)}
        prompt = prompts.title_prompt([{"id": k, "source": p["source_name"], "title": p["title"]}
                                       for k, p in zip(id_map, batch)])
        try:
            response = client.generate_json(prompt, system=prompts.SYSTEM)
        except LLMAuthError:
            raise
        except LLMError as exc:
            log.error("Title classification batch %d failed: %s", start // batch_size + 1, exc)
            continue
        if isinstance(response, dict):
            response = next((v for v in response.values() if isinstance(v, list)), [])
        for item in response if isinstance(response, list) else []:
            uid = id_map.pop(str(item.get("id", "")).strip(), None)
            market = normalise_market(item.get("market_focus")) if uid else None
            if market:
                results.append({"uid": uid, "status": "title_only", "score": None, "market_focus": market,
                                "topic": str(item.get("topic", "")).strip()[:60],
                                "reason": str(item.get("reason", "")).strip()[:400]})
    log.info("Classified %d/%d papers without an abstract from their title", len(results), len(papers))
    return results


# --- 3. Selection -----------------------------------------------------------------
def select(scored: list[dict], cfg: dict) -> list[dict]:
    """Pick 5-8 papers: highest scores first, at most `max_per_source` per source.

    If too few papers clear `min_score`, the threshold is relaxed so the issue
    still reaches `min_papers`. If no EM paper made the cut but a qualifying one
    exists, it replaces the lowest-scored pick, so EM coverage is not crowded out
    by the larger volume of DM research.
    """
    s = cfg["selection"]
    ranked = sorted(scored, key=lambda p: (p["score"], p.get("published") or ""), reverse=True)

    def pick(pool, min_score, per_source):
        chosen, per = [], Counter()
        for p in pool:
            if len(chosen) >= s["max_papers"]:
                break
            if p["score"] >= min_score and per[p["source_id"]] < per_source:
                chosen.append(p)
                per[p["source_id"]] += 1
        return chosen

    chosen = pick(ranked, s["min_score"], s["max_per_source"])
    if len(chosen) < s["min_papers"]:
        chosen = pick(ranked, 0, s["max_per_source"])
    if len(chosen) < s["min_papers"]:
        chosen = pick(ranked, 0, s["max_papers"])

    if chosen and not any(p["market_focus"] == "EM" for p in chosen):
        per = Counter(p["source_id"] for p in chosen)
        em = next((p for p in ranked if p["market_focus"] == "EM" and p["score"] >= s["min_score"]
                   and p not in chosen and per[p["source_id"]] < s["max_per_source"]), None)
        if em and len(chosen) >= s["min_papers"]:
            chosen[-1] = em
            chosen.sort(key=lambda p: p["score"], reverse=True)
    return chosen


# --- 4. Deep analysis -------------------------------------------------------------
def _validate_analysis(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("analysis is not a JSON object")
    market = normalise_market(data.get("market_focus"))
    takeaways = [str(t).strip() for t in data.get("key_takeaways") or [] if str(t).strip()]
    required = {"summary": data.get("summary"), "relevance": data.get("relevance"),
                "market_focus_rationale": data.get("market_focus_rationale")}
    if market is None or len(takeaways) < 2 or not all(isinstance(v, str) and v.strip()
                                                        for v in required.values()):
        raise ValueError("analysis is missing required fields")
    confidence = str(data.get("confidence", "medium")).lower()
    return {
        "summary": required["summary"].strip(),
        "key_takeaways": takeaways[:3],
        "relevance": required["relevance"].strip(),
        "market_focus": market,
        "market_focus_rationale": required["market_focus_rationale"].strip(),
        "regions": [str(r) for r in data.get("regions") or []][:8],
        "topics": [str(t) for t in data.get("topics") or []][:3],
        "confidence": confidence if confidence in {"high", "medium", "low"} else "medium",
    }


def analyse_paper(paper: dict, client: JSONModel, full_text=None) -> dict:
    """Write one newsletter entry. `full_text` (a fulltext.FullText) is used when
    the paper is openly available; otherwise the entry rests on the abstract."""
    prompt = prompts.analysis_prompt({
        "source": paper["source_name"], "title": paper["title"],
        "authors": paper.get("authors"), "abstract": paper["abstract"],
        "full_text": full_text.text if full_text else None,
    })
    basis = ({"basis": "full text", "full_text_url": full_text.url, "full_text_kind": full_text.kind,
              "full_text_chars": len(full_text.text)} if full_text else {"basis": "abstract"})
    last_error = None
    result = None
    for _ in range(2):  # one retry if the model omits required fields
        try:
            result = _validate_analysis(client.generate_json(prompt, system=prompts.SYSTEM))
            break
        except ValueError as exc:
            last_error = exc
            log.warning("Invalid analysis for '%s': %s", paper["title"][:60], exc)
    if result is None:
        raise LLMError(f"Could not get a valid analysis: {last_error}")

    # Number grounding: every number in the summary and takeaways must appear in
    # the text the model was given. If not, ask once for a rewrite without it.
    source = full_text.text if full_text else (paper.get("abstract") or "")
    missing = quality.unverified_numbers(result, source)
    if missing:
        log.warning("'%s': numbers not in the source text %s; asking for a rewrite",
                    paper["title"][:60], missing)
        retry = prompt + prompts.number_feedback(missing)
        try:
            second = _validate_analysis(client.generate_json(retry, system=prompts.SYSTEM))
            if len(quality.unverified_numbers(second, source)) < len(missing):
                result = second
        except (ValueError, LLMError) as exc:
            log.warning("Rewrite failed for '%s': %s", paper["title"][:60], exc)
        missing = quality.unverified_numbers(result, source)
    claimed = quality.numbers_in(" ".join([result.get("summary", "")] + list(result.get("key_takeaways", []))))
    return {**result, **basis, "numbers": {"checked": len(set(claimed)), "unverified": missing}}


# --- Periods ------------------------------------------------------------------------
# An issue covers one calendar month: the October issue holds research published
# 1-31 October. It is closed on the publish day of the next month (the 5th by
# default, `schedule.publish_day`), so papers that reach a source a few days late
# still make it into their own month. Until then, the month is shown on the
# dashboard with a provisional selection, as is the month in progress.
#
# Late arrivals: a paper dated in the previous month that only reached a source
# (or OpenAlex) after that month's issue went out is not lost; it joins the next
# issue. Before the first issue there is nothing to be late for.

def publish_day(cfg: dict) -> int:
    return int((cfg.get("schedule") or {}).get("publish_day", 5))

def month_of(now: datetime) -> str:
    return now.strftime("%Y-%m")


def previous_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def month_bounds(month: str) -> tuple[str, str]:
    """First day of the month and first day of the next, as ISO datetimes (UTC)."""
    return f"{month}-01T00:00:00+00:00", f"{next_month(month)}-01T00:00:00+00:00"


def paper_date(p: dict) -> str:
    """The date that places a paper in a month: publication date, else when first seen."""
    return (p.get("published") or p.get("fetched_at") or "")[:10]


def month_pool(storage: Storage, month: str, rebuilding: bool = False) -> tuple[list[dict], list[str]]:
    """Papers that belong to `month`'s issue: those dated in the month, plus late
    arrivals from the month before. Returns (papers, uids of the late arrivals).
    `rebuilding=True` when (re)making this month's own issue: papers that arrived
    after an earlier version of it are then included rather than passed on."""
    issues = storage.get_issues()
    in_other = {u for i in issues if i["id"] != month for u in i["paper_uids"]}
    prev = previous_month(month)
    prev_issue = next((i for i in issues if i["id"] == prev), None)
    this_issue = next((i for i in issues if i["id"] == month), None)
    pool, late = [], []
    for p in storage.all_papers():
        d = paper_date(p)
        if d[:7] == month:
            # arrived after this month's issue went out: it is a late arrival for the next one
            if this_issue and not rebuilding and p["uid"] not in this_issue["paper_uids"] \
                    and p["fetched_at"] > this_issue["published_at"]:
                continue
            if p["uid"] not in in_other:
                pool.append(p)
        elif (prev_issue and d[:7] == prev and p["uid"] not in in_other
              and p["fetched_at"] > prev_issue["published_at"]):
            pool.append(p)
            late.append(p["uid"])
    pool.sort(key=paper_date, reverse=True)
    return pool, late


def draft_month(storage: Storage, cfg: dict, now: datetime | None = None) -> str | None:
    """The month whose write-ups should be drafted today, or None.

    The day before the publish day (the 4th), the month that just ended is all but
    complete, so its provisional picks are written up in advance. The issue on the
    5th then only confirms the selection (papers that arrived on the 4th can still
    enter) and writes up any newcomer, so it is ready early and needs few calls to
    the stronger model on publish day."""
    now = now or datetime.now(timezone.utc)
    if now.day != publish_day(cfg) - 1:
        return None
    month = previous_month(month_of(now))
    issue = storage.get_issue(month)
    due = datetime.fromisoformat(f"{month_of(now)}-{publish_day(cfg):02d}T00:00:00+00:00")
    if issue and datetime.fromisoformat(issue["published_at"]) >= due:
        return None
    return month


def provisional_periods(storage: Storage, cfg: dict, now: datetime | None = None) -> list[dict]:
    """Months not yet published, newest first: the month in progress, and, until
    the publish day, the month that just ended and is still collecting late papers."""
    now = now or datetime.now(timezone.utc)
    out = [provisional(storage, cfg, now, month_of(now))]
    prev = previous_month(month_of(now))
    if now.day < publish_day(cfg) and not storage.get_issue(prev):
        out.append(provisional(storage, cfg, now, prev))
    return out


def provisional(storage: Storage, cfg: dict, now: datetime | None = None, month: str | None = None) -> dict:
    """The selection a month not yet published would get if its issue were made today."""
    now = now or datetime.now(timezone.utc)
    month = month or month_of(now)
    pool, late = month_pool(storage, month, rebuilding=True)
    screened = storage.get_screening()
    scored = [{**p, **screened[p["uid"]]} for p in pool if screened.get(p["uid"], {}).get("status") == "scored"]
    picks = select(scored, cfg) if scored else []
    start, end = month_bounds(month)
    closing = month != month_of(now)
    y, m = map(int, next_month(month).split("-"))
    return {"id": month, "title": datetime.fromisoformat(start).strftime("%B %Y"),
            "window_start": start, "window_end": end if closing else now.isoformat(),
            "closing": closing,
            "publish_on": datetime(y, m, publish_day(cfg), tzinfo=timezone.utc).isoformat(),
            "paper_uids": [p["uid"] for p in picks], "pool_uids": [p["uid"] for p in pool],
            "late_uids": late, "n_scored": len(scored)}


# --- Orchestration ----------------------------------------------------------------
def run_screening(storage: Storage, client: JSONModel, cfg: dict, now: datetime | None = None) -> dict:
    """Pre-filter and score every paper in the window not screened yet.

    This is the weekly job: scores accumulate through the month, so the monthly
    run only has to select and write up. Returns counts for the run."""
    now = now or datetime.now(timezone.utc)
    # This month and the last: the issue being built and the one about to close
    # (late arrivals for it are screened too).
    since = month_bounds(previous_month(month_of(now)))[0]
    screened = storage.get_screening()
    candidates = storage.candidates(since)
    # Papers excluded earlier for lack of an abstract are re-checked against the
    # (cheap, rule-based) pre-filter, so new rules such as "dataset" apply to them too.
    stale = [p for p in candidates if screened.get(p["uid"], {}).get("reason") == "no abstract available"]
    storage.save_screening([e for e in prefilter(stale, cfg)[1]
                            if e["reason"] != "no abstract available"], model=None)
    # New papers, plus title-only papers whose abstract has since been recovered.
    min_chars = cfg["prefilter"]["min_abstract_chars"]
    new = [p for p in candidates if p["uid"] not in screened
           or (screened[p["uid"]]["status"] == "title_only" and len(p.get("abstract") or "") >= min_chars)]
    kept, excluded = prefilter(new, cfg)
    storage.save_screening(excluded, model=None)
    model = lambda: getattr(client, "model", None)
    results = screen(kept, client, cfg, on_batch=lambda res: storage.save_screening(res, model=model()))
    # Papers still without an abstract get a market-focus label from their title.
    screened = storage.get_screening()
    no_abstract = [p for p in candidates if screened.get(p["uid"], {}).get("reason") == "no abstract available"]
    titled = classify_titles(no_abstract, client, cfg) if no_abstract else []
    storage.save_screening(titled, model=model())
    return {"new": len(new), "excluded": len(excluded), "scored": len(results),
            "unscored": len(kept) - len(results), "title_only": len(titled)}


def run_analysis(storage: Storage, client: JSONModel, cfg: dict,
                 now: datetime | None = None, analysis_client: JSONModel | None = None,
                 rewrite: bool = False, fulltext_finder=None, month: str | None = None) -> dict:
    """`client` screens; `analysis_client` (defaults to `client`) writes the full
    analyses — a fast, cheap model is enough to score, a stronger one writes.
    `rewrite=True` regenerates the write-ups of the selected papers (e.g. after a
    prompt change, or when a stronger model is available again).
    `fulltext_finder` (a fulltext.FullTextFinder) lets the write-ups use the open
    full text of each selected paper; without it they use the abstract.
    `month` ("YYYY-MM") is the issue to build; by default the month that just
    ended, so a run on 1 November (or a re-run on 3 November) builds October."""
    now = now or datetime.now(timezone.utc)
    analysis_client = analysis_client or client
    month = month or previous_month(month_of(now))
    start, end = month_bounds(month)

    # 1-2. pre-filter and screen papers not seen before (also run daily on its own)
    run_screening(storage, client, cfg, now)
    candidates, late = month_pool(storage, month, rebuilding=True)
    screened = storage.get_screening()

    # 3. select from everything scored in the month
    scored = [{**p, **screened[p["uid"]]} for p in candidates
              if screened.get(p["uid"], {}).get("status") == "scored"]
    selected = select(scored, cfg)

    # 4. deep analysis (cached)
    entries = []
    if selected:
        log.info("Selected %d papers; writing up those without a cached analysis", len(selected))
    for i, p in enumerate(selected, 1):
        analysis = None if rewrite else storage.get_analysis(p["uid"])
        preferred = (getattr(analysis_client, "candidates", None) or [None])[0]
        if analysis is not None and preferred and analysis.get("model") not in (None, preferred):
            # Written by a fallback model on a busy day: try again with the preferred
            # one (the earlier write-up is kept if that fails too).
            log.info("Rewriting '%s' (cached write-up is by the fallback model %s)",
                     p["title"][:60], analysis.get("model"))
            analysis = None
        if analysis is None:
            getattr(analysis_client, "prefer_first", lambda: None)()
            log.info("Writing up %d/%d with %s: %s", i, len(selected),
                     getattr(analysis_client, "model", "LLM"), p["title"][:70])
            full_text = None
            if fulltext_finder is not None:
                full_text = fulltext_finder.find(p)
                log.info("  %s", f"full text: {full_text.kind}, {len(full_text.text):,} characters"
                         if full_text else "no open full text: writing from the abstract")
            try:
                analysis = analyse_paper(p, analysis_client, full_text)
            except LLMAuthError:
                raise
            except LLMError as exc:
                previous = storage.get_analysis(p["uid"])
                if previous is None:
                    log.error("Skipping '%s': %s", p["title"][:60], exc)
                    continue
                log.warning("Keeping the previous write-up of '%s': %s", p["title"][:60], exc)
                entries.append({"paper": p, "analysis": previous})
                continue
            storage.save_analysis(p["uid"], analysis, getattr(analysis_client, "model", None))
        entries.append({"paper": p, "analysis": analysis})

    # coverage report: what was considered, and which sources needed fallbacks
    status = {p["uid"]: screened.get(p["uid"], {}) for p in candidates}
    by_source: dict[str, Counter] = {}
    for p in candidates:
        c = by_source.setdefault(p["source_id"], Counter())
        c["candidates"] += 1
        if p.get("abstract_source"):
            c["abstract recovered"] += 1
        st = status[p["uid"]]
        if st.get("status") == "scored":
            c["scored"] += 1
        elif st.get("status") == "title_only":
            c["classified from title"] += 1
        elif st.get("reason"):
            c["excluded: " + st["reason"]] += 1
    return {
        "month": month,
        "window_start": start,
        "window_end": end,
        "late_uids": late,
        "pool_uids": [p["uid"] for p in candidates],
        "n_candidates": len(candidates),
        "n_scored": len(scored),
        "coverage": {k: dict(v) for k, v in sorted(by_source.items())},
        "entries": entries,
    }
