"""Evaluation of the EM / DM / Transversal classification against human labels.

Workflow
  1. `python main.py export-labels` draws a stratified sample of screened papers
     and writes evaluation/labels.csv with title + abstract only. The LLM's labels
     are deliberately NOT in the file, so the annotator is not anchored by them.
  2. A human fills the `human_label` column, following the definitions in
     src/prompts.py.
  3. `python main.py evaluate` compares the LLM's labels with the human labels.

Design notes
  - Stratified by the LLM's label: DM papers far outnumber EM ones, so a random
    sample would contain too few EM papers for a stable EM recall.
  - A fixed dev/test split: use `dev` errors to improve the prompt, then report on
    `test`, which was not looked at while tuning (avoids overfitting the prompt
    to the evaluation set).
  - Metrics: accuracy with a Wilson 95% interval, confusion matrix, per-class
    precision/recall/F1, macro-F1 (robust to class imbalance) and Cohen's kappa
    (agreement beyond chance).
"""

from __future__ import annotations

import csv
import math
import random
import time
from collections import Counter
from pathlib import Path

from src.analysis import normalise_market, screen
from src.llm import JSONModel
from src.storage import Storage

LABELS = ["EM", "DM", "Transversal"]
FIELDS = ["id", "split", "source", "title", "abstract", "human_label", "notes"]


# --- export --------------------------------------------------------------------
def export_sample(storage: Storage, path: Path, per_class: int = 15, seed: int = 42,
                  test_share: float = 1 / 3) -> Counter:
    """Write a blind, stratified labelling sample. Returns counts per LLM class."""
    papers = {p["uid"]: p for p in storage.all_papers()}
    screened = [s for s in storage.get_screening().values()
                if s["status"] == "scored" and s["uid"] in papers]
    rng = random.Random(seed)
    sample, counts = [], Counter()
    for label in LABELS:
        pool = sorted((s for s in screened if s["market_focus"] == label), key=lambda s: s["uid"])
        picked = rng.sample(pool, min(per_class, len(pool)))
        n_test = round(len(picked) * test_share)
        for i, s in enumerate(picked):
            sample.append((s["uid"], "test" if i < n_test else "dev"))
        counts[label] = len(picked)
    rng.shuffle(sample)  # mix classes so their order gives nothing away

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM: opens cleanly in Excel
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for uid, split in sample:
            p = papers[uid]
            w.writerow({"id": uid, "split": split, "source": p["source_name"], "title": p["title"],
                        "abstract": p["abstract"], "human_label": "", "notes": ""})
    return counts


def read_labels(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        raw = (r.get("human_label") or "").strip()
        if raw.lower() in ("t", "trans"):
            raw = "Transversal"
        label = normalise_market(raw)
        if label:
            out.append({**r, "human_label": label})
    return out


# --- metrics -------------------------------------------------------------------
def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def compute_metrics(pairs: list[tuple[str, str]]) -> dict:
    """pairs = [(human, llm), ...]"""
    n = len(pairs)
    cm = {h: {p: 0 for p in LABELS} for h in LABELS}
    for h, p in pairs:
        cm[h][p] += 1
    correct = sum(cm[l][l] for l in LABELS)
    per_class = {}
    for l in LABELS:
        tp = cm[l][l]
        pred = sum(cm[h][l] for h in LABELS)
        actual = sum(cm[l].values())
        prec = tp / pred if pred else None
        rec = tp / actual if actual else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else (0.0 if actual else None)
        per_class[l] = {"precision": prec, "recall": rec, "f1": f1, "support": actual}
    f1s = [v["f1"] for v in per_class.values() if v["f1"] is not None]
    po = correct / n if n else 0.0
    pe = sum(sum(cm[l].values()) * sum(cm[h][l] for h in LABELS) for l in LABELS) / (n * n) if n else 0.0
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {
        "n": n, "accuracy": po, "accuracy_ci": wilson_interval(correct, n),
        "macro_f1": sum(f1s) / len(f1s) if f1s else 0.0, "kappa": kappa,
        "per_class": per_class, "confusion": cm,
    }


# --- evaluation ------------------------------------------------------------------
def evaluate(storage: Storage, path: Path, client: JSONModel | None = None,
             cfg: dict | None = None) -> dict:
    """Compare LLM labels with human labels.

    By default uses the labels stored at screening time. If `client` is given, the
    labelled papers are re-classified now with the *current* prompt — use this
    after changing the prompt.
    """
    labelled = read_labels(path)
    if not labelled:
        raise ValueError(f"No human labels found in {path}. Fill the 'human_label' column "
                         "with EM, DM or Transversal.")
    screened = storage.get_screening()
    if client is not None:
        papers = {p["uid"]: p for p in storage.all_papers()}
        to_run = [papers[r["id"]] for r in labelled if r["id"] in papers]
        fresh = screen(to_run, client, cfg)
        llm = {r["uid"]: r["market_focus"] for r in fresh}
        source = f"re-classified now ({getattr(client, 'model', '?')}, current prompt)"
    else:
        llm = {uid: s["market_focus"] for uid, s in screened.items() if s.get("market_focus")}
        source = "labels stored at screening time"

    rows = [{**r, "llm_label": llm[r["id"]], "llm_reason": screened.get(r["id"], {}).get("reason", "")}
            for r in labelled if r["id"] in llm]
    result = {"source": source, "n_labelled": len(labelled), "splits": {}}
    for split in ("all", "dev", "test"):
        subset = rows if split == "all" else [r for r in rows if r.get("split") == split]
        if subset:
            result["splits"][split] = compute_metrics([(r["human_label"], r["llm_label"]) for r in subset])
    result["errors"] = [r for r in rows if r["human_label"] != r["llm_label"]]
    result["stage_agreement"] = stage_agreement(storage)
    return result


def stage_agreement(storage: Storage) -> dict:
    """Consistency check needing no human labels: for papers that got a full
    write-up, how often do the screening model and the writing model, labelling
    independently, agree?"""
    screened = storage.get_screening()
    pairs = [(a["uid"], screened[a["uid"]]["market_focus"], a["market_focus"])
             for a in storage.all_analyses() if a["uid"] in screened]
    return {"n": len(pairs), "agree": sum(s == w for _, s, w in pairs),
            "disagreements": [p for p in pairs if p[1] != p[2]]}


def format_report(result: dict) -> str:
    def pct(x):
        return "  n/a" if x is None else f"{x * 100:5.1f}%"

    lines = [f"LLM labels: {result['source']}",
             f"Human-labelled papers: {result['n_labelled']}", ""]
    for split, m in result["splits"].items():
        lo, hi = m["accuracy_ci"]
        lines += [f"== {split.upper()} (n={m['n']}) ==",
                  f"Accuracy   {pct(m['accuracy'])}   (95% CI {lo * 100:.0f}-{hi * 100:.0f}%)",
                  f"Macro-F1   {m['macro_f1']:.3f}",
                  f"Cohen's κ  {m['kappa']:.3f}", ""]
        if split == "all":
            lines.append("                 precision  recall     F1   support")
            for l, v in m["per_class"].items():
                f1 = "  n/a" if v["f1"] is None else f"{v['f1']:.2f}"
                lines.append(f"  {l:<12}   {pct(v['precision'])}   {pct(v['recall'])}   {f1}   {v['support']:>5}")
            lines += ["", "Confusion matrix (rows = human, columns = LLM)",
                      "               " + "".join(f"{l:>13}" for l in LABELS)]
            for h in LABELS:
                lines.append(f"  {h:<12} " + "".join(f"{m['confusion'][h][p]:>13}" for p in LABELS))
            lines.append("")
    sa = result["stage_agreement"]
    if sa["n"]:
        lines += [f"Screening vs write-up model agreement: {sa['agree']}/{sa['n']} "
                  f"({sa['agree'] / sa['n'] * 100:.0f}%)", ""]
    if result["errors"]:
        lines.append(f"Disagreements ({len(result['errors'])}):")
        for r in result["errors"]:
            lines.append(f"  [{r.get('split', '')}] human={r['human_label']:<11} llm={r['llm_label']:<11} "
                         f"{r['title'][:70]}")
            if r.get("notes"):
                lines.append(f"        your note: {r['notes']}")
    return "\n".join(lines)


# --- two-model agreement (no human labels needed) -------------------------------------
AGREEMENT_FIELDS = ["id", "source", "title", "abstract", "label_a", "label_b",
                    "reason_a", "reason_b", "human_label"]


def model_agreement(storage: Storage, client: JSONModel, cfg: dict, per_class: int = 35,
                    seed: int = 7, batch_size: int = 15, rounds: int = 4, pause: float = 90,
                    min_coverage: float = 0.9) -> dict:
    """Re-label a stratified sample with a second, independent model and compare.

    Model A is the screening model (labels stored at screening time); model B is
    `client`, a different and stronger model, run now with the same prompt and
    the same inputs (title, source, abstract). Agreement is not accuracy: two
    models can share a mistake. It does show how stable the labels are, which
    classes the models confuse, and which papers a human should look at first
    (the disagreements, written out with both models' reasons).

    The sample is stratified by model A's label so EM is not crowded out; the
    agreement rate is also re-weighted by each class's share of all screened
    papers, to estimate agreement on the full population."""
    papers = {p["uid"]: p for p in storage.all_papers()}
    screened = [s for s in storage.get_screening().values()
                if s["status"] == "scored" and s["uid"] in papers and s.get("market_focus")]
    shares = Counter(s["market_focus"] for s in screened)
    rng = random.Random(seed)
    sample = []
    for label in LABELS:
        pool = sorted((s for s in screened if s["market_focus"] == label), key=lambda s: s["uid"])
        sample += rng.sample(pool, min(per_class, len(pool)))
    rng.shuffle(sample)        # mixed batches: a failed batch must not remove one class
    by_uid = {s["uid"]: s for s in sample}

    # The larger free-tier models are often overloaded (HTTP 503): retry the
    # batches that failed, after a pause, a few times.
    b: dict[str, dict] = {}
    bcfg = {**cfg, "screening": {**cfg["screening"], "batch_size": batch_size}}

    def keep(results):
        for r in results:
            b[r["uid"]] = {**r, "model": getattr(client, "model", "?")}
    for attempt in range(rounds):
        todo = [papers[s["uid"]] for s in sample if s["uid"] not in b]
        if not todo:
            break
        if attempt:
            time.sleep(pause)
        getattr(client, "prefer_first", lambda: None)()
        screen(todo, client, bcfg, on_batch=keep)
    if len(b) < min_coverage * len(sample):
        raise ValueError(f"Only {len(b)} of {len(sample)} papers were re-labelled (the second model "
                         "was unavailable); no report written. Try again later.")
    rows = []
    for uid, r in b.items():
        a, p = by_uid[uid], papers[uid]
        rows.append({"id": uid, "source": p["source_name"], "title": p["title"], "abstract": p["abstract"],
                     "label_a": a["market_focus"], "label_b": r["market_focus"],
                     "reason_a": a.get("reason", ""), "reason_b": r.get("reason", ""), "human_label": "",
                     "score_a": a.get("score"), "score_b": r.get("score")})
    if not rows:
        raise ValueError("The second model returned no labels.")

    m = compute_metrics([(r["label_b"], r["label_a"]) for r in rows])     # rows = model B
    per_class = {}
    for label in LABELS:
        sub = [r for r in rows if r["label_a"] == label]
        if sub:
            per_class[label] = sum(r["label_a"] == r["label_b"] for r in sub) / len(sub)
    total = sum(shares[l] for l in per_class)
    weighted = sum(per_class[l] * shares[l] / total for l in per_class) if total else None
    scored = [(r["score_a"], r["score_b"]) for r in rows if r["score_a"] is not None and r["score_b"] is not None]
    return {
        "model_a": sorted({s.get("model") for s in sample if s.get("model")}),
        "model_b": ", ".join(f"{m} ({n})" for m, n in Counter(r["model"] for r in b.values()).most_common()),
        "n_sample": len(sample), "n": len(rows), "metrics": m,
        "agree_by_class_a": per_class, "population_shares": dict(shares), "weighted_agreement": weighted,
        "score_mad": sum(abs(x - y) for x, y in scored) / len(scored) if scored else None,
        "score_within_1": sum(abs(x - y) <= 1 for x, y in scored) / len(scored) if scored else None,
        "rows": rows,
    }


def write_disagreements(result: dict, path: Path) -> int:
    rows = [r for r in result["rows"] if r["label_a"] != r["label_b"]]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=AGREEMENT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def format_agreement(result: dict) -> str:
    m = result["metrics"]
    lo, hi = m["accuracy_ci"]
    a = ", ".join(result["model_a"]) or "screening model"
    lines = [
        "EM / DM / Transversal classification: agreement between two independent models",
        "",
        f"Model A (screening, labels stored when each paper was scored): {a}",
        f"Model B (stronger model, re-labelled now with the same prompt and inputs; papers per model): {result['model_b']}",
        f"Sample: {result['n']} of {result['n_sample']} papers drawn, stratified by model A's label "
        f"(up to {max(Counter(r['label_a'] for r in result['rows']).values())} per class)",
        "",
        "This measures agreement, not accuracy: two models can make the same mistake.",
        "Disagreements are listed in evaluation/disagreements.csv with both models'",
        "reasons, as the papers a human reviewer should check first.",
        "",
        f"Agreement        {m['accuracy'] * 100:5.1f}%   (95% CI {lo * 100:.0f}-{hi * 100:.0f}%)",
        f"Cohen's kappa    {m['kappa']:.3f}   (agreement beyond chance; >0.8 is usually read as strong)",
    ]
    if result["weighted_agreement"] is not None:
        shares = result["population_shares"]
        tot = sum(shares.values())
        lines.append(f"Re-weighted      {result['weighted_agreement'] * 100:5.1f}%   (by class share of all "
                     f"{tot} screened papers: " + ", ".join(f"{l} {shares.get(l, 0) / tot * 100:.0f}%" for l in LABELS) + ")")
    lines += ["", "Agreement by model A's label:"]
    for l, v in result["agree_by_class_a"].items():
        n = sum(r["label_a"] == l for r in result["rows"])
        lines.append(f"  {l:<12} {v * 100:5.1f}%   (n={n})")
    lines += ["", "Confusion matrix (rows = model B, columns = model A)",
              "               " + "".join(f"{l:>13}" for l in LABELS)]
    for h in LABELS:
        lines.append(f"  {h:<12} " + "".join(f"{m['confusion'][h][p]:>13}" for p in LABELS))
    if result["score_mad"] is not None:
        lines += ["", "Relevance score (0-10), same papers:",
                  f"  mean absolute difference {result['score_mad']:.2f} points; "
                  f"within 1 point: {result['score_within_1'] * 100:.0f}%"]
    dis = [r for r in result["rows"] if r["label_a"] != r["label_b"]]
    if dis:
        lines += ["", f"Disagreements ({len(dis)}):"]
        for r in dis:
            lines.append(f"  A={r['label_a']:<11} B={r['label_b']:<11} {r['title'][:72]}")
    return "\n".join(lines)
