"""Freeze the current selection into a numbered monthly issue.

An issue covers one calendar month and is named after it: the run on 1 November
produces the "October 2026" issue (research published 1-31 October, plus late
arrivals from September). Re-publishing the same month replaces that issue;
earlier months are never touched.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from src import prompts
from src.llm import JSONModel, LLMAuthError, LLMError
from src.storage import Storage

log = logging.getLogger(__name__)


def issue_id_and_title(window_start: str, window_end: str) -> tuple[str, str]:
    start = datetime.fromisoformat(window_start)
    end = datetime.fromisoformat(window_end)
    mid = start + (end - start) / 2
    return mid.strftime("%Y-%m"), mid.strftime("%B %Y")


def fallback_intro(entries: list[dict]) -> str:
    focus = Counter(e["analysis"]["market_focus"] for e in entries)
    parts = [f"{n} {label}" for label, n in focus.most_common()]
    topics = Counter(t for e in entries for t in e["analysis"].get("topics", []))
    top = ", ".join(t.lower() for t, _ in topics.most_common(3))
    return (f"This issue features {len(entries)} papers ({', '.join(parts)}). "
            + (f"Recurring themes: {top}." if top else ""))


def write_intro(entries: list[dict], client: JSONModel | None) -> str:
    if client is None:
        return fallback_intro(entries)
    try:
        data = client.generate_json(prompts.intro_prompt([
            {"title": e["paper"]["title"], "source": e["paper"]["source_name"],
             "market_focus": e["analysis"]["market_focus"],
             "summary": e["analysis"]["summary"]} for e in entries
        ]), system=prompts.SYSTEM)
        intro = str(data.get("intro", "")).strip() if isinstance(data, dict) else ""
        return intro or fallback_intro(entries)
    except LLMAuthError:
        raise
    except LLMError as exc:
        log.warning("Could not write the editor's note (%s); using a plain summary", exc)
        return fallback_intro(entries)


def publish_issue(storage: Storage, report: dict, client: JSONModel | None = None) -> dict:
    entries = report["entries"]
    if not entries:
        raise ValueError("No papers were selected, so there is nothing to publish.")
    if report.get("month"):
        issue_id = report["month"]
        title = datetime.fromisoformat(f"{issue_id}-01").strftime("%B %Y")
    else:
        issue_id, title = issue_id_and_title(report["window_start"], report["window_end"])
    issue = {
        "id": issue_id,
        "title": title,
        "intro": write_intro(entries, client),
        "paper_uids": [e["paper"]["uid"] for e in entries],
        "stats": {
            "window_start": report["window_start"],
            "window_end": report["window_end"],
            "pool_uids": report.get("pool_uids", []),
            "late_uids": report.get("late_uids", []),
            "n_candidates": report["n_candidates"],
            "n_scored": report["n_scored"],
            "coverage": report["coverage"],
            "models": sorted({e["analysis"].get("model") for e in entries if e["analysis"].get("model")}),
        },
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.save_issue(issue)
    return issue
