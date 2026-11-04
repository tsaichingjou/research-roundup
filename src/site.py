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
from datetime import datetime, timedelta, timezone
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


def _last_day(end_iso: str) -> str:
    """Windows end at midnight on the 1st of the next month: show the last day included."""
    end = datetime.fromisoformat(end_iso)
    if end.hour == end.minute == 0 and end.day == 1:
        end = end - timedelta(days=1)
    return end.strftime("%-d %b %Y")


def collect(storage: Storage, sources_cfg: dict) -> dict:
    """Assemble everything the site needs into one JSON-serialisable dict."""
    sources = {s["id"]: _source_entry(s["id"], s["name"], s.get("group", "Other"), s["category"])
               for s in sources_cfg["sources"] if s.get("enabled", True) and not s.get("aggregator") and not s.get("institutions")}
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
            "abstract": p["abstract"], "abstract_source": p.get("abstract_source"),
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


FALLBACK_COLUMNS = [("OpenAlex", "OpenAlex"), ("publisher page", "Publisher page"),
                    ("opening of the text", "Paper's own text"), ("title only", "Title only")]


def fallback_table(papers: list[dict], sources: dict) -> list[dict]:
    """Per source, how many papers needed a fallback because the feed had no
    abstract: recovered from OpenAlex, the publisher's page or the paper's own
    text, or left with the title only (labelled, not scored, not selectable).
    Only sources that needed one are listed, most affected first."""
    rows = {}
    for p in papers:
        if p.get("status") == "excluded" and p.get("reason") != "no abstract available":
            continue                                    # not research: not counted
        r = rows.setdefault(p["source"], {"source": sources.get(p["source"], {}).get("name", p["source"]),
                                          "total": 0, **{k: 0 for k, _ in FALLBACK_COLUMNS}})
        r["total"] += 1
        if p.get("abstract_source") in r:
            r[p["abstract_source"]] += 1
        if p.get("status") in ("title_only", "excluded"):
            r["title only"] += 1
    out = [r for r in rows.values() if any(r[k] for k, _ in FALLBACK_COLUMNS)]
    return sorted(out, key=lambda r: (-sum(r[k] for k, _ in FALLBACK_COLUMNS), r["source"]))


def fallback_markdown(rows: list[dict]) -> str:
    head = "| Source | Papers | " + " | ".join(name for _, name in FALLBACK_COLUMNS) + " |"
    lines = [head, "|---|---:|" + "---:|" * len(FALLBACK_COLUMNS)]
    for r in rows:
        lines.append(f"| {r['source']} | {r['total']} | "
                     + " | ".join(str(r[k] or "–") for k, _ in FALLBACK_COLUMNS) + " |")
    return "\n".join(lines)


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
    recovered = sum(c.get("abstract recovered", 0) for c in cov.values())
    pool = set(issue["stats"].get("pool_uids") or [])
    if not pool:   # issues made before calendar-month windows: fall back to dates
        st = issue["stats"]
        pool = {p["uid"] for p in data["papers"]
                if st["window_start"][:10] <= (p["published"] or p["fetched_at"])[:10] < st["window_end"][:10]}
    fallbacks = fallback_table([p for p in data["papers"] if p["uid"] in pool], data["sources"])
    title_only = sorted(((c.get("classified from title", 0), data["sources"].get(src, {}).get("name", src))
                         for src, c in cov.items() if c.get("classified from title")), reverse=True)
    return {
        "site": site, "issue": issue, "entries": entries, "focus": FOCUS, "recovered": recovered,
        "n_title_only": sum(n for n, _ in title_only),
        "title_only_sources": ", ".join(f"{n} from {name}" for n, name in title_only[:4])
                              + (", and others" if len(title_only) > 4 else ""),
        "n_full": sum(e["analysis"].get("basis") == "full text" for e in entries),
        "sources": data["sources"],
        "composition": [(k, composition[k]) for k in FOCUS if composition[k]],
        "n_sources": len(cov),
        "excluded": excluded,
        "window": f"{_date(issue['stats']['window_start'])} to {_last_day(issue['stats']['window_end'])}",
        "n_late": len(issue["stats"].get("late_uids") or []),
        "fmt_date": _date,
        "fallbacks": fallbacks, "fallback_columns": FALLBACK_COLUMNS,
    }


def add_periods(data: dict, in_progress: list[dict] | dict | None) -> None:
    """Give the dashboard its periods (the month in progress, then each issue) and
    tag every paper with the one period it belongs to."""
    periods = []
    if isinstance(in_progress, dict):
        in_progress = [in_progress]
    for ip in in_progress or []:
        if any(i["id"] == ip["id"] for i in data["issues"]):
            continue
        periods.append({**{k: ip.get(k) for k in ("id", "title", "window_start", "window_end",
                                                  "paper_uids", "late_uids", "closing", "publish_on")},
                        "status": "in_progress", "pool": set(ip["pool_uids"])})
    for i in data["issues"]:
        st = i["stats"]
        pool = set(st.get("pool_uids") or [])
        if not pool:   # issues made before calendar-month windows: fall back to dates
            pool = {p["uid"] for p in data["papers"]
                    if st["window_start"][:10] <= (p["published"] or p["fetched_at"])[:10] < st["window_end"][:10]}
        periods.append({"id": i["id"], "title": i["title"], "window_start": st["window_start"],
                        "window_end": st["window_end"], "paper_uids": i["paper_uids"],
                        "late_uids": st.get("late_uids", []), "status": "published", "pool": pool})
    for p in data["papers"]:
        p["period"] = next((x["id"] for x in periods if p["uid"] in x["pool"]), None)
    for x in periods:
        x["n_pool"] = len(x.pop("pool"))
    data["periods"] = periods


def build_site(storage: Storage, sources_cfg: dict, site_cfg: dict,
               out_dir: Path | None = None, selection: dict | None = None,
               in_progress: list[dict] | dict | None = None, publish_day: int | None = None) -> dict:
    out = Path(out_dir or ROOT / site_cfg.get("output_dir", "docs"))
    (out / "issues").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    data = collect(storage, sources_cfg)
    data["selection"] = selection
    add_periods(data, in_progress)

    for issue in data["issues"]:
        html = env.get_template("newsletter.html.j2").render(**issue_context(data, issue, site_cfg))
        (out / "issues" / f"{issue['id']}.html").write_text(html, encoding="utf-8")

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out / "data" / "site.json").write_text(payload, encoding="utf-8")
    embedded = payload.replace("</", "<\\/")  # safe inside a <script> element
    html = env.get_template("dashboard.html.j2").render(site=site_cfg, data_json=embedded,
                                                        publish_day=int(publish_day or 5),
                                                        has_issues=bool(data["issues"]))
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("")  # serve files as-is on GitHub Pages
    return {"out_dir": out, "issues": [i["id"] for i in data["issues"]], "papers": len(data["papers"])}
