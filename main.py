"""The Research Roundup — command-line entry point.

Usage
    python main.py fetch                  # fetch all enabled sources
    python main.py fetch --only nber bis  # fetch selected sources
    python main.py show --limit 10        # list the most recent stored papers
    python main.py models                 # list Gemini models available to your API key
    python main.py check                  # test each configured model and its response time
    python main.py analyse                # screen, select and analyse this month's papers
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.analysis import load_analysis_config, run_analysis  # noqa: E402
from src.env import load_dotenv  # noqa: E402
from src.llm import GeminiClient, LLMError  # noqa: E402
from src.pipeline import load_config, run_fetch  # noqa: E402
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
        extra = ", ".join(f"{k} {v}" for k, v in c.items() if k.startswith("excluded"))
        print(f"  {src:<8} {c.get('candidates', 0):>4} candidates, {c.get('scored', 0):>4} scored"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="The Research Roundup agent")
    parser.add_argument("--config", default=ROOT / "config" / "sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch new papers from enabled sources")
    p_fetch.add_argument("--only", nargs="+", help="Source ids to fetch")

    p_show = sub.add_parser("show", help="Show most recent stored papers")
    p_show.add_argument("--limit", type=int, default=10)

    sub.add_parser("models", help="List Gemini models available to your API key")
    sub.add_parser("check", help="Test each configured Gemini model")
    sub.add_parser("analyse", aliases=["analyze"], help="Screen, select and analyse papers")

    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    config = load_config(args.config)
    storage = Storage(ROOT / config["settings"]["database_path"])

    if args.command == "fetch":
        results = run_fetch(config, storage, only=args.only)
        print("\nSummary")
        print("-" * 60)
        for r in results:
            status = f"{r.n_fetched:>4} fetched, {r.n_new:>4} new" if r.ok else f"ERROR: {r.error[:60]}"
            print(f"  {r.source_id:<10} {status}")
        print(f"\nTotal papers in database: {storage.count()}")
        return 0 if any(r.ok for r in results) else 1

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
            report = run_analysis(storage, client, acfg, analysis_client=writer)
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
