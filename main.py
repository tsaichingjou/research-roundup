"""The Research Roundup — command-line entry point.

Usage
    python main.py fetch                  # fetch all enabled sources
    python main.py fetch --only nber bis  # fetch selected sources
    python main.py check-sources          # test every source feed without storing anything
    python main.py enrich                 # recover missing abstracts (also runs after fetch)
    python main.py show --limit 10        # list the most recent stored papers
    python main.py models                 # list Gemini models available to your API key
    python main.py check                  # test each configured model and its response time
    python main.py screen                 # weekly job: score new papers, no write-ups
    python main.py analyse                # screen, select and analyse this month's papers
    python main.py analyse --rewrite      # regenerate the write-ups (e.g. after a prompt change)
    python main.py export-labels          # write a blind labelling sample to evaluation/labels.csv
    python main.py evaluate [--rerun]     # compare LLM classification with your labels
    python main.py publish                # analyse, freeze this month's issue and build the site
    python main.py build                  # rebuild the site (docs/) from the database, no LLM calls
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.analysis import load_analysis_config, run_analysis, run_screening  # noqa: E402
from src.enrich import run_enrichment  # noqa: E402
from src.env import load_dotenv  # noqa: E402
from src.evaluation import evaluate, export_sample, format_report  # noqa: E402
from src.issues import publish_issue  # noqa: E402
from src.llm import GeminiClient, LLMError  # noqa: E402
from src.pipeline import check_sources, load_config, run_fetch  # noqa: E402
from src.site import build_site  # noqa: E402
from src.storage import Storage  # noqa: E402


def make_client(acfg: dict, stage: str) -> GeminiClient:
    llm = acfg["llm"]
    return GeminiClient(models=llm[f"{stage}_models"], temperature=llm.get("temperature", 0.2),
                        min_interval_seconds=llm.get("min_interval_seconds", 4),
                        timeout=llm.get("timeout_seconds", 90))


def print_analysis(report: dict) -> None:
    print(f"\nWindow: {report['window_start'][:10]} -> {report['window_end'][:10]}")
    print(f"Candidates: {report['n_candidates']}, scored: {report['n_scored']}")
    print("\nCoverage by source")
    print("-" * 70)
    for src, c in report["coverage"].items():
        extra = ", ".join(f"{k} {v}" for k, v in c.items() if k.startswith(("excluded", "abstract")))
        print(f"  {src:<18} {c.get('candidates', 0):>4} candidates, {c.get('scored', 0):>4} scored"
              + (f"  ({extra})" if extra else ""))
    print(f"\nSelected for this issue: {len(report['entries'])}")
    print("=" * 70)
    for i, e in enumerate(report["entries"], 1):
        p, a = e["paper"], e["analysis"]
        print(f"\n{i}. [{a['market_focus']}] {p['title']}")
        print(f"   {p['source_name']} | score {p['score']}/10 | {', '.join(a['topics'])}")
        print(f"   Summary: {a['summary']}")
        for t in a["key_takeaways"]:
            print(f"     - {t}")
        print(f"   Why it matters: {a['relevance']}")
        print(f"   Classification: {a['market_focus_rationale']}")


def print_enrichment(summary: dict) -> None:
    found = sum(summary["found"].values())
    print(f"\nMissing abstracts: {summary['tried']} papers looked up, {found} recovered"
          + (f" ({', '.join(f'{n} from {k}' for k, n in summary['found'].items())})" if found else ""))
    for src, c in sorted(summary["by_source"].items(), key=lambda kv: -kv[1]["tried"]):
        print(f"  {src[:46]:<46} {c['found']:>4} / {c['tried']:<4} recovered")
    if summary.get("note"):
        print(f"  Note: OpenAlex stopped early ({summary['note']}). Papers not found will be retried later.")


def main() -> int:
    parser = argparse.ArgumentParser(description="The Research Roundup agent")
    parser.add_argument("--config", default=ROOT / "config" / "sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch new papers from enabled sources")
    p_fetch.add_argument("--only", nargs="+", help="Source ids to fetch")
    p_fetch.add_argument("--no-enrich", action="store_true", help="Skip recovering missing abstracts")
    sub.add_parser("enrich", help="Recover missing abstracts from OpenAlex and publisher pages")

    p_chk = sub.add_parser("check-sources", help="Test every source feed without storing anything")
    p_chk.add_argument("--only", nargs="+", help="Source ids to check")

    p_show = sub.add_parser("show", help="Show most recent stored papers")
    p_show.add_argument("--limit", type=int, default=10)

    sub.add_parser("models", help="List Gemini models available to your API key")
    sub.add_parser("check", help="Test each configured Gemini model")
    sub.add_parser("screen", help="Weekly job: pre-filter and score new papers (no write-ups)")
    p_an = sub.add_parser("analyse", aliases=["analyze"], help="Screen, select and analyse papers")
    p_an.add_argument("--rewrite", action="store_true",
                      help="Regenerate the write-ups of the selected papers")

    p_ex = sub.add_parser("export-labels", help="Write a blind, stratified labelling sample")
    p_ex.add_argument("--per-class", type=int, default=15)
    p_ex.add_argument("--force", action="store_true", help="Overwrite an existing labels file")
    p_ev = sub.add_parser("evaluate", help="Compare LLM classification with human labels")
    p_ev.add_argument("--rerun", action="store_true",
                      help="Re-classify the labelled papers now with the current prompt")

    sub.add_parser("publish", help="Analyse, freeze this month's issue and build the site")
    sub.add_parser("build", help="Rebuild the static site from the database")

    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    config = load_config(args.config)
    # The scheduled GitHub Actions runs use their own database and site folder
    # (RR_DATABASE_PATH, RR_SITE_DIR) so they never clash with a local copy.
    storage = Storage(ROOT / os.environ.get("RR_DATABASE_PATH", config["settings"]["database_path"]))

    if args.command == "fetch":
        results = run_fetch(config, storage, only=args.only)
        print("\nSummary")
        print("-" * 60)
        for r in results:
            status = f"{r.n_fetched:>4} fetched, {r.n_new:>4} new" if r.ok else f"ERROR: {r.error[:60]}"
            print(f"  {r.source_id:<18} {status}")
            for name, n in list(r.breakdown.items())[:15]:
                print(f"      {n:>4}  {name}")
            if len(r.breakdown) > 15:
                print(f"      ... and {len(r.breakdown) - 15} more institutions")
        print(f"\nTotal papers in database: {storage.count()}")
        if not args.no_enrich:
            print_enrichment(run_enrichment(storage, config["settings"],
                                            load_analysis_config(ROOT / "config" / "analysis.yaml")))
        return 0 if any(r.ok for r in results) else 1

    if args.command == "enrich":
        print_enrichment(run_enrichment(storage, config["settings"],
                                        load_analysis_config(ROOT / "config" / "analysis.yaml")))
        return 0

    if args.command == "check-sources":
        rows = check_sources(config, only=args.only)
        print(f"\n{'source':<18}{'status':<8}{'items':>6}{'abstract':>10}  newest")
        print("-" * 60)
        for r in rows:
            if r["ok"]:
                print(f"{r['id']:<18}{'OK':<8}{r['n']:>6}{r['with_abstract']:>10}  {r['newest']}")
                if r.get("diagnostics"):
                    for name, msg in r["diagnostics"].items():
                        print(f"{'':<20}{name[:40]:<40} {msg}")
                else:
                    for name, n in r.get("institutions", [])[:25]:
                        print(f"{'':<20}{n:>4}  {name}")
            else:
                print(f"{r['id']:<18}{'FAIL':<8}  {r['error']}")
        bad = [r["id"] for r in rows if not r["ok"] or r["n"] == 0]
        print(f"\n{len(rows) - len(bad)}/{len(rows)} sources returned items."
              + (f" Check: {', '.join(bad)}" if bad else ""))
        return 0

    site_cfg = yaml.safe_load((ROOT / "config" / "site.yaml").read_text(encoding="utf-8"))

    def build() -> int:
        selection = load_analysis_config(ROOT / "config" / "analysis.yaml")["selection"]
        out_dir = ROOT / os.environ.get("RR_SITE_DIR", site_cfg.get("output_dir", "site"))
        res = build_site(storage, config, site_cfg, out_dir=out_dir, selection=selection)
        out = res["out_dir"].relative_to(ROOT)
        print(f"Site built in {out}/: {len(res['issues'])} issue(s), {res['papers']} papers.")
        print(f"Open {out}/index.html in a browser to preview it.")
        return 0

    if args.command == "build":
        return build()

    if args.command == "publish":
        acfg = load_analysis_config(ROOT / "config" / "analysis.yaml")
        try:
            client, writer = make_client(acfg, "screening"), make_client(acfg, "analysis")
            report = run_analysis(storage, client, acfg, analysis_client=writer)
            issue = publish_issue(storage, report, client=writer)
        except (LLMError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 1
        print(f"Published the {issue['title']} issue with {len(issue['paper_uids'])} papers.")
        print(f"Editor's note: {issue['intro']}")
        return build()

    labels_path = ROOT / "evaluation" / "labels.csv"
    if args.command == "export-labels":
        if labels_path.exists() and not args.force:
            print(f"{labels_path.relative_to(ROOT)} already exists and may contain your labels. "
                  "Use --force to overwrite it.")
            return 1
        counts = export_sample(storage, labels_path, per_class=args.per_class)
        print(f"Wrote {sum(counts.values())} papers to {labels_path.relative_to(ROOT)} "
              f"(stratified by LLM label: {dict(counts)})")
        print("Fill the 'human_label' column with EM, DM or Transversal, then run: python main.py evaluate")
        return 0

    if args.command == "evaluate":
        acfg = load_analysis_config(ROOT / "config" / "analysis.yaml")
        try:
            client = make_client(acfg, "screening") if args.rerun else None
            result = evaluate(storage, labels_path, client=client, cfg=acfg)
        except (LLMError, ValueError, FileNotFoundError) as exc:
            print(f"ERROR: {exc}")
            return 1
        report = format_report(result)
        print(report)
        (ROOT / "evaluation" / "report.txt").write_text(report + "\n", encoding="utf-8")
        print("\nSaved to evaluation/report.txt")
        return 0

    if args.command == "screen":
        acfg = load_analysis_config(ROOT / "config" / "analysis.yaml")
        try:
            r = run_screening(storage, make_client(acfg, "screening"), acfg)
        except LLMError as exc:
            print(f"ERROR: {exc}")
            return 1
        print(f"\nScreening: {r['new']} new papers, {r['scored']} scored, {r['excluded']} excluded"
              + (f", {r['unscored']} left for the next run" if r["unscored"] else ""))
        return 0

    if args.command in ("models", "check", "analyse", "analyze"):
        acfg = load_analysis_config(ROOT / "config" / "analysis.yaml")
        try:
            client = make_client(acfg, "screening")
            if args.command == "models":
                print("\n".join(client.list_models()))
                return 0
            if args.command == "check":
                for stage in ("screening", "analysis"):
                    print(f"\n{stage} models:")
                    for model in acfg["llm"][f"{stage}_models"]:
                        ok, secs, msg = client.ping(model)
                        print(f"  {'OK  ' if ok else 'FAIL'} {model:<28} {secs:5.1f}s  {msg}")
                return 0
            writer = make_client(acfg, "analysis")
            report = run_analysis(storage, client, acfg, analysis_client=writer,
                                  rewrite=getattr(args, "rewrite", False))
        except LLMError as exc:
            print(f"\nERROR: {exc}")
            return 1
        out = ROOT / "output" / "latest_analysis.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print_analysis(report)
        print(f"\nLLM calls this run: {client.calls} screening ({client.model}), "
              f"{writer.calls} analysis ({writer.model})")
        print(f"Full results saved to {out.relative_to(ROOT)}")
        return 0

    if args.command == "show":
        for row in storage.recent(args.limit):
            date = (row["published"] or row["fetched_at"])[:10]
            print(f"[{row['source_id']:<5}] {date}  {row['title'][:90]}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
