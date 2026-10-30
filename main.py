"""The Research Roundup — command-line entry point.

Usage
    python main.py fetch                  # fetch all enabled sources
    python main.py fetch --only nber bis  # fetch selected sources
    python main.py show --limit 10        # list the most recent stored papers
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.pipeline import load_config, run_fetch  # noqa: E402
from src.storage import Storage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="The Research Roundup agent")
    parser.add_argument("--config", default=ROOT / "config" / "sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch new papers from enabled sources")
    p_fetch.add_argument("--only", nargs="+", help="Source ids to fetch")

    p_show = sub.add_parser("show", help="Show most recent stored papers")
    p_show.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
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

    if args.command == "show":
        for row in storage.recent(args.limit):
            date = (row["published"] or row["fetched_at"])[:10]
            print(f"[{row['source_id']:<5}] {date}  {row['title'][:90]}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
