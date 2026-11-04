"""Keeping LLM quality up over time.

Three layers, none of which needs a human in the loop:

1. Regression check when the system changes. A frozen benchmark of 105 papers
   (evaluation/benchmark.csv: the stratified sample of the first agreement
   check, with the label two independent models agreed on) is re-labelled with
   the current prompt and screening model whenever the prompt or the model
   configuration changes. If agreement with the reference labels falls below a
   threshold, a GitHub issue is opened.
2. A monthly health report. After each issue is published, a short report in
   English compares the month with the months before it (class mix, scores,
   fallback-model use, full-text share, unverified numbers, screening
   coverage, and whether the screening and writing models agree on the
   selected papers). It is posted as a GitHub issue, which GitHub emails to the
   repository owner, and kept in evaluation/quality/.
3. Number grounding in every write-up. Each number in a summary or key
   takeaway must appear in the text the model was given (full text or
   abstract). If one does not, the model is asked once to rewrite without it;
   any number still unverified is recorded and counted in the monthly report.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LABELS = ["EM", "DM", "Transversal"]

# --- 3. number grounding ---------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")
_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
          "7": "seven", "8": "eight", "9": "nine", "10": "ten", "11": "eleven", "12": "twelve",
          "15": "fifteen", "20": "twenty", "30": "thirty", "40": "forty", "50": "fifty",
          "100": "hundred"}


def numbers_in(text: str) -> list[str]:
    """Numeric tokens in a text, normalised (thousands separators removed)."""
    out = []
    for m in _NUM.findall(text or ""):
        n = m.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".") or "0"
        out.append(n)
    return out


def unverified_numbers(write_up: dict, source_text: str) -> list[str]:
    """Numbers in the summary and key takeaways that do not appear in the source text."""
    claimed = numbers_in(" ".join([write_up.get("summary", "")] + list(write_up.get("key_takeaways", []))))
    available = set(numbers_in(source_text))
    lower = (source_text or "").lower()
    missing = []
    for n in dict.fromkeys(claimed):
        if n in available:
            continue
        if n in _WORDS and re.search(rf"\b{_WORDS[n]}\b", lower):
            continue                     # "10 currencies" for "ten currencies"
        missing.append(n)
    return missing


# --- 1. regression check against a frozen benchmark -------------------------------------
BENCHMARK_FIELDS = ["id", "source", "title", "abstract", "label_a", "label_b", "reference"]


def write_benchmark(rows: list[dict], path: Path) -> None:
    """Freeze an agreement sample: the reference label is the one both models gave
    (left empty where they disagreed, so disputed papers do not count)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=BENCHMARK_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "reference": r["label_a"] if r["label_a"] == r["label_b"] else ""})


def read_benchmark(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def regression_check(client, cfg: dict, path: Path, threshold: float = 0.88) -> dict:
    """Re-label the benchmark with the current prompt and model and compare with
    the reference labels."""
    from src.analysis import screen

    bench = read_benchmark(path)
    papers = [{"uid": r["id"], "source_name": r["source"], "title": r["title"], "abstract": r["abstract"]}
              for r in bench]
    got = {}
    for _ in range(3):                        # retry batches that failed (overloaded model)
        todo = [p for p in papers if p["uid"] not in got]
        if not todo:
            break
        for r in screen(todo, client, cfg):
            got[r["uid"]] = r["market_focus"]
    scored = [r for r in bench if r["reference"] and r["id"] in got]
    changed = [{**r, "now": got[r["id"]]} for r in scored if got[r["id"]] != r["reference"]]
    rate = 1 - len(changed) / len(scored) if scored else 0.0
    per_class = {}
    for label in LABELS:
        sub = [r for r in scored if r["reference"] == label]
        if sub:
            per_class[label] = sum(got[r["id"]] == label for r in sub) / len(sub)
    complete = len(got) >= 0.9 * len(bench)
    return {"model": getattr(client, "model", "?"), "n": len(scored), "n_benchmark": len(bench),
            "agreement": rate, "per_class": per_class, "changed": changed, "threshold": threshold,
            "complete": complete, "ok": complete and rate >= threshold,
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def format_regression(res: dict) -> str:
    status = ("✅ Passed" if res["ok"] else
              "⚠️ Incomplete: the model was unavailable for part of the benchmark" if not res["complete"] else
              "⚠️ Below threshold")
    lines = [
        f"## LLM regression check: {status}",
        "",
        f"Run {res['run_at']} with `{res['model']}` and the current prompt, on the frozen benchmark "
        f"(`evaluation/benchmark.csv`, {res['n_benchmark']} papers; {res['n']} with an agreed reference label).",
        "",
        f"- Agreement with the reference labels: **{res['agreement'] * 100:.1f}%** "
        f"(alert below {res['threshold'] * 100:.0f}%)",
    ]
    for label, v in res["per_class"].items():
        lines.append(f"  - {label}: {v * 100:.1f}%")
    if res["changed"]:
        lines += ["", f"Papers whose label changed ({len(res['changed'])}):", ""]
        for r in res["changed"][:30]:
            lines.append(f"- {r['reference']} → {r['now']}: {r['title'][:90]}")
    lines += ["", "Re-run after any change to `src/prompts.py` or the models in `config/analysis.yaml`. "
              "A drop means the change altered how papers are classified: check the changed papers "
              "before keeping it."]
    return "\n".join(lines)


# --- 2. monthly health report -------------------------------------------------------------
def _share(papers: list[dict]) -> dict:
    c = Counter(p["market_focus"] for p in papers)
    n = sum(c.values()) or 1
    return {l: c.get(l, 0) / n for l in LABELS}


def monthly_report(storage, cfg: dict, month: str, site_url: str = "",
                   regression_path: Path | None = None, mention: str = "") -> dict:
    """Health report for one published issue, in English (Markdown)."""
    from src.analysis import month_pool, paper_date, previous_month

    issue = storage.get_issue(month)
    if issue is None:
        raise ValueError(f"No issue for {month}")
    screened = storage.get_screening()
    preferred = cfg["llm"]["analysis_models"][0]

    pool, _ = month_pool(storage, month, rebuilding=True)
    pool_s = [{**p, **screened[p["uid"]]} for p in pool if p["uid"] in screened]
    scored = [p for p in pool_s if p["status"] == "scored"]
    title_only = [p for p in pool_s if p["status"] == "title_only"]
    pending = [p for p in pool if p["uid"] not in screened]

    # Baseline: scored papers dated in the three months before this one.
    prev = [previous_month(month)]
    for _ in range(2):
        prev.append(previous_month(prev[-1]))
    base = [s for s in screened.values() if s["status"] == "scored"]
    papers = {p["uid"]: p for p in storage.all_papers()}
    base = [s for s in base if s["uid"] in papers and paper_date(papers[s["uid"]])[:7] in prev]

    entries = [(u, storage.get_analysis(u) or {}) for u in issue["paper_uids"]]
    stage = [(screened.get(u, {}).get("market_focus"), a.get("market_focus")) for u, a in entries if a]
    stage_agree = sum(x == y for x, y in stage)
    fallback = [a.get("model") for _, a in entries if a and a.get("model") not in (None, preferred)]
    full = sum(a.get("basis") == "full text" for _, a in entries)
    nums_checked = sum((a.get("numbers") or {}).get("checked", 0) for _, a in entries)
    nums_bad = [(papers.get(u, {}).get("title", u), n) for u, a in entries
                for n in (a.get("numbers") or {}).get("unverified", [])]

    mix, mix_base = _share(scored), _share(base)
    mean = sum(p["score"] for p in scored) / len(scored) if scored else 0.0
    mean_base = sum(s["score"] for s in base) / len(base) if base else None

    checks = []   # (ok, name, detail)
    n = len(stage)
    checks.append((n == 0 or stage_agree >= 0.75 * n, "Classification consistency",
                   f"screening and write-up models agree on {stage_agree}/{n} selected papers"))
    if len(base) >= 30 and len(scored) >= 30:      # too few papers make shares meaningless
        drift = max(abs(mix[l] - mix_base[l]) for l in LABELS)
        em_gone = mix["EM"] == 0 and mix_base["EM"] >= 0.05
        checks.append((drift <= 0.15 and not em_gone, "Market-focus mix",
                       " / ".join(f"{l} {mix[l] * 100:.0f}%" for l in LABELS)
                       + " (previous 3 months: " + " / ".join(f"{mix_base[l] * 100:.0f}%" for l in LABELS) + ")"))
        checks.append((abs(mean - mean_base) <= 1.0, "Relevance scores",
                       f"mean {mean:.1f} (previous 3 months: {mean_base:.1f})"))
    else:
        checks.append((True, "Market-focus mix", " / ".join(f"{l} {mix[l] * 100:.0f}%" for l in LABELS)
                       + " (too few papers, or no earlier months, to compare with)"))
        checks.append((True, "Relevance scores", f"mean {mean:.1f} (too few papers, or no earlier months, to compare with)"))
    checks.append((not fallback, "Write-up model",
                   f"{len(entries) - len(fallback)}/{len(entries)} by {preferred}"
                   + (f"; {len(fallback)} by a fallback model ({', '.join(sorted(set(fallback)))}), "
                      "retried automatically until the 8th" if fallback else "")))
    checks.append((True, "Full-text write-ups",
                   f"{full}/{len(entries)} written from the paper's full text, the rest from the abstract"))
    if any("numbers" in a for _, a in entries):
        checks.append((not nums_bad, "Numbers checked against the source",
                       f"{nums_checked - len(nums_bad)}/{nums_checked} numbers in summaries and takeaways found in the text"))
    else:
        checks.append((True, "Numbers checked against the source",
                       "not available: these write-ups were made before the check existed"))
    total = len(pool_s) + len(pending)
    checks.append((len(pending) <= 0.1 * max(total, 1), "Screening coverage",
                   f"{len(scored)} scored, {len(title_only)} labelled from title only (no abstract), "
                   f"{len(pending)} not yet screened, of {total} publications "
                   f"(the rest left out as not research)"))

    bad = [c for c in checks if not c[0]]
    title_month = datetime.fromisoformat(f"{month}-01").strftime("%B %Y")
    status = "✅ All checks passed" if not bad else (f"⚠️ {len(bad)} checks need attention" if len(bad) > 1 else "⚠️ 1 check needs attention")
    lines = [f"## {status}", "",
             f"Automatic quality report for the **{title_month}** issue "
             f"(published {issue['published_at'][:16].replace('T', ' ')} UTC).", "",
             "| | Check | Result |", "|---|---|---|"]
    lines += [f"| {'✅' if ok else '⚠️'} | {name} | {detail} |" for ok, name, detail in checks]
    if nums_bad:
        lines += ["", "Numbers not found in the source text (check these in the newsletter):", ""]
        lines += [f"- {n} in “{t[:80]}”" for t, n in nums_bad]
    if regression_path and regression_path.exists():
        first = regression_path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
        lines += ["", f"Last benchmark regression check: {first} (see `evaluation/regression.md`)."]
    if site_url:
        lines += ["", f"Newsletter: {site_url.rstrip('/')}/issues/{month}.html · Dashboard: {site_url}"]
    lines += ["", "What the checks mean: see *Keeping LLM quality up* in the README."]
    if mention:
        lines += ["", f"cc @{mention}"]
    return {"title": f"LLM quality check: {title_month} issue", "body": "\n".join(lines),
            "ok": not bad, "published_at": issue["published_at"]}
