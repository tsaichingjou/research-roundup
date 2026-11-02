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

from src import prompts
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
    for _ in range(2):  # one retry if the model omits required fields
        try:
            return {**_validate_analysis(client.generate_json(prompt, system=prompts.SYSTEM)), **basis}
        except ValueError as exc:
            last_error = exc
            log.warning("Invalid analysis for '%s': %s", paper["title"][:60], exc)
    raise LLMError(f"Could not get a valid analysis: {last_error}")


# --- Orchestration ----------------------------------------------------------------
def run_screening(storage: Storage, client: JSONModel, cfg: dict, now: datetime | None = None) -> dict:
    """Pre-filter and score every paper in the window not screened yet.

    This is the weekly job: scores accumulate through the month, so the monthly
    run only has to select and write up. Returns counts for the run."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=cfg["window"]["days"])).isoformat()
    screened = storage.get_screening()
    candidates = storage.candidates(since)
    # Papers excluded earlier for lack of an abstract are re-checked against the
    # (cheap, rule-based) pre-filter, so new rules such as "dataset" apply to them too.
    stale = [p for p in candidates if screened.get(p["uid"], {}).get("reason") == "no abstract available"]
    storage.save_screening([e for e in prefilter(stale, cfg)[1]
                            if e["reason"] != "no abstract available"], model=None)
    new = [p for p in candidates if p["uid"] not in screened]
    kept, excluded = prefilter(new, cfg)
    storage.save_screening(excluded, model=None)
    results = screen(kept, client, cfg,
                     on_batch=lambda res: storage.save_screening(res, model=getattr(client, "model", None)))
    return {"new": len(new), "excluded": len(excluded), "scored": len(results),
            "unscored": len(kept) - len(results)}


def run_analysis(storage: Storage, client: JSONModel, cfg: dict,
                 now: datetime | None = None, analysis_client: JSONModel | None = None,
                 rewrite: bool = False, fulltext_finder=None) -> dict:
    """`client` screens; `analysis_client` (defaults to `client`) writes the full
    analyses — a fast, cheap model is enough to score, a stronger one writes.
    `rewrite=True` regenerates the write-ups of the selected papers (e.g. after a
    prompt change, or when a stronger model is available again).
    `fulltext_finder` (a fulltext.FullTextFinder) lets the write-ups use the open
    full text of each selected paper; without it they use the abstract."""
    now = now or datetime.now(timezone.utc)
    analysis_client = analysis_client or client
    since = (now - timedelta(days=cfg["window"]["days"])).isoformat()

    # 1-2. pre-filter and screen papers not seen before (also run weekly on its own)
    run_screening(storage, client, cfg, now)
    candidates = storage.candidates(since)
    screened = storage.get_screening()

    # 3. select from everything scored in the window
    scored = [{**p, **screened[p["uid"]]} for p in candidates
              if screened.get(p["uid"], {}).get("status") == "scored"]
    selected = select(scored, cfg)

    # 4. deep analysis (cached)
    entries = []
    if selected:
        log.info("Selected %d papers; writing up those without a cached analysis", len(selected))
    for i, p in enumerate(selected, 1):
        analysis = None if rewrite else storage.get_analysis(p["uid"])
        if analysis is None:
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
                log.error("Skipping '%s': %s", p["title"][:60], exc)
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
        elif st.get("reason"):
            c["excluded: " + st["reason"]] += 1
    return {
        "window_start": since,
        "window_end": now.isoformat(),
        "n_candidates": len(candidates),
        "n_scored": len(scored),
        "coverage": {k: dict(v) for k, v in sorted(by_source.items())},
        "entries": entries,
    }
