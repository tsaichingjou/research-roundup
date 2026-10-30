"""Fetch stage of the pipeline: load config -> fetch every enabled source -> store.

A failure in one source is logged and recorded, never allowed to stop the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    ids = [s["id"] for s in config.get("sources", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source ids in sources.yaml")
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
            results.append(SourceResult(source["id"], ok=True, n_fetched=len(papers), n_new=n_new))
        except Exception as exc:  # one bad source must not stop the others
            log.error("[%s] failed: %s", source["id"], exc)
            storage.log_fetch(source["id"], "error", message=str(exc))
            results.append(SourceResult(source["id"], ok=False, error=str(exc)))

    return results
