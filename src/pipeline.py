"""Fetch stage of the pipeline: load config -> fetch every enabled source -> store.

A failure in one source is logged and recorded, never allowed to stop the run.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.fetchers import FETCHERS
from src.storage import Storage

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    source_id: str
    ok: bool
    n_fetched: int = 0
    n_new: int = 0
    error: str = ""
    breakdown: dict = field(default_factory=dict)   # per institution, for aggregated feeds


def load_config(path: str | Path) -> dict:
    """Load sources.yaml, plus institutions.yaml (from the same folder) for
    aggregated feeds such as the BIS Central Bank Research Hub."""
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    ids = [s["id"] for s in config.get("sources", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source ids in sources.yaml")
    inst_path = path.parent / "institutions.yaml"
    config["institutions"] = []
    if inst_path.exists():
        config["institutions"] = yaml.safe_load(inst_path.read_text(encoding="utf-8"))["institutions"]
    for s in config["sources"]:
        if s.get("aggregator") or s.get("institutions"):
            s["_institutions"] = config["institutions"]
    return config


def run_fetch(config: dict, storage: Storage, only: list[str] | None = None) -> list[SourceResult]:
    settings = config.get("settings", {})
    results: list[SourceResult] = []

    for source in config["sources"]:
        if not source.get("enabled", True):
            continue
        if only and source["id"] not in only:
            continue

        fetcher = FETCHERS.get(source["type"])
        if fetcher is None:
            msg = f"No fetcher registered for type '{source['type']}'"
            log.error("[%s] %s", source["id"], msg)
            storage.log_fetch(source["id"], "error", message=msg)
            results.append(SourceResult(source["id"], ok=False, error=msg))
            continue

        try:
            papers = fetcher(source, settings)
            n_new = storage.save_papers(papers)
            storage.log_fetch(source["id"], "ok", len(papers), n_new)
            log.info("[%s] fetched %d entries, %d new", source["id"], len(papers), n_new)
            result = SourceResult(source["id"], ok=True, n_fetched=len(papers), n_new=n_new)
            if source.get("aggregator") or source.get("institutions"):
                result.breakdown = dict(Counter(p.source_name for p in papers).most_common())
            results.append(result)
        except Exception as exc:  # one bad source must not stop the others
            log.error("[%s] failed: %s", source["id"], exc)
            storage.log_fetch(source["id"], "error", message=str(exc))
            results.append(SourceResult(source["id"], ok=False, error=str(exc)))

    return results


def check_sources(config: dict, only: list[str] | None = None) -> list[dict]:
    """Fetch every enabled source without storing anything, and report what it
    returns: item count, newest date, how many items have a usable abstract and,
    for aggregated feeds, which institutions appear. Used to verify new sources."""
    settings = config.get("settings", {})
    rows = []
    for source in config["sources"]:
        if not source.get("enabled", True) or (only and source["id"] not in only):
            continue
        row = {"id": source["id"], "group": source.get("group", "")}
        try:
            papers = FETCHERS[source["type"]](source, settings)
            dates = [p.published for p in papers if p.published]
            row.update(ok=True, n=len(papers),
                       newest=max(dates).date().isoformat() if dates else "-",
                       with_abstract=sum(len(p.abstract) >= 200 for p in papers))
            if source.get("aggregator") or source.get("institutions"):
                row["institutions"] = Counter(p.source_name for p in papers).most_common()
            if source.get("_diagnostics"):
                row["diagnostics"] = source["_diagnostics"]
        except Exception as exc:  # report, never stop
            row.update(ok=False, error=str(exc)[:120])
        rows.append(row)
    return rows
