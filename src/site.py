"""Static site builder: one email-ready newsletter per issue + an interactive explorer.

Output (default `docs/`, served by GitHub Pages):
    index.html             interactive explorer (all data embedded, works offline)
    issues/<YYYY-MM>.html  the newsletter for each issue (inline styles, email-safe)
    data/site.json         the same data as JSON, for anyone who wants it
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.storage import Storage

ROOT = Path(__file__).resolve().parent.parent

FOCUS = {
    "EM": {"name": "Emerging markets", "color": "#a8680a", "tint": "#f7efe3"},
    "DM": {"name": "Developed markets", "color": "#2c5bb8", "tint": "#e8eef9"},
    "Transversal": {"name": "Transversal", "color": "#18876b", "tint": "#e3f3ee"},
}


GROUP_ORDER = ["Top-5 economics journals", "Research institutions", "Federal Reserve System",
               "G10 central banks and ECB", "European national central banks", "Other central banks"]


def short_name(name: str) -> str:
    """Compact label for charts and lists."""
    m = re.search(r"\(([^)]+)\)$", name)
    if m:
        return m.group(1)
    if name == "Board of Governors of the Federal Reserve System":
        return "Fed Board"
    return name.replace("Federal Reserve Bank of ", "Fed ")


def _source_entry(sid: str, name: str, group: str, category: str) -> dict:
    return {"id": sid, "name": name, "short": short_name(name), "group": group, "category": category,
            "group_rank": GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)}


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).strftime("%-d %b %Y")


def collect(storage: Storage, sources_cfg: dict) -> dict:
    """Assemble everything the site needs into one JSON-serialisable dict."""
    sources = {s["id"]: _source_entry(s["id"], s["name"], s.get("group", "Other"), s["category"])
               for s in sources_cfg["sources"] if s.get("enabled", True) and not s.get("aggregator")}
    for inst in sources_cfg.get("institutions", []):
        if not inst.get("skip"):
            sources[inst["id"]] = _source_entry(inst["id"], inst["name"], inst["group"], "central_bank")
    screening = storage.get_screening()
    issues = storage.get_issues()
    featured_in: dict[str, list[str]] = {}
    for issue in issues:
        for uid in issue["paper_uids"]:
            featured_in.setdefault(uid, []).append(issue["id"])

    papers = []
    for p in storage.all_papers():
        sources.setdefault(p["source_id"], _source_entry(p["source_id"], p["source_name"],
                                                         "Other central banks", "central_bank"))
        s = screening.get(p["uid"], {})
        a = storage.get_analysis(p["uid"]) if p["uid"] in featured_in else None
        papers.append({
            "uid": p["uid"], "title": p["title"], "url": p["url"],
            "source": p["source_id"], "authors": p["authors"],
            "published": p["published"], "fetched_at": p["fetched_at"],
            "abstract": p["abstract"],
            "status": s.get("status", "pending"),
            "score": s.get("score"), "focus": (a or {}).get("market_focus") or s.get("market_focus"),
            "topic": s.get("topic"), "reason": s.get("reason"),
            "analysis": a, "issues": featured_in.get(p["uid"], []),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "focus": FOCUS,
        "issues": [{k: v for k, v in i.items()} for i in issues],
        "papers": papers,
    }


def issue_context(data: dict, issue: dict, site: dict) -> dict:
    by_uid = {p["uid"]: p for p in data["papers"]}
    entries = [by_uid[u] for u in issue["paper_uids"] if u in by_uid and by_uid[u]["analysis"]]
    composition = Counter(e["analysis"]["market_focus"] for e in entries)
    cov = issue["stats"]["coverage"]
    excluded = []
    for src, c in cov.items():
        for k, v in c.items():
            if k.startswith("excluded"):
                excluded.append({"source": data["sources"].get(src, {}).get("name", src),
                                 "reason": k.removeprefix("excluded: "), "n": v})
    return {
        "site": site, "issue": issue, "entries": entries, "focus": FOCUS,
        "sources": data["sources"],
        "composition": [(k, composition[k]) for k in FOCUS if composition[k]],
        "n_sources": len(cov),
        "excluded": excluded,
        "window": f"{_date(issue['stats']['window_start'])} to {_date(issue['stats']['window_end'])}",
        "fmt_date": _date,
    }


def build_site(storage: Storage, sources_cfg: dict, site_cfg: dict,
               out_dir: Path | None = None) -> dict:
    out = Path(out_dir or ROOT / site_cfg.get("output_dir", "docs"))
    (out / "issues").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    data = collect(storage, sources_cfg)

    for issue in data["issues"]:
        html = env.get_template("newsletter.html.j2").render(**issue_context(data, issue, site_cfg))
        (out / "issues" / f"{issue['id']}.html").write_text(html, encoding="utf-8")

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out / "data" / "site.json").write_text(payload, encoding="utf-8")
    embedded = payload.replace("</", "<\\/")  # safe inside a <script> element
    html = env.get_template("dashboard.html.j2").render(site=site_cfg, data_json=embedded,
                                                        has_issues=bool(data["issues"]))
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("")  # serve files as-is on GitHub Pages
    return {"out_dir": out, "issues": [i["id"] for i in data["issues"]], "papers": len(data["papers"])}
