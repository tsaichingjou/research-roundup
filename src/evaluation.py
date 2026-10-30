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
