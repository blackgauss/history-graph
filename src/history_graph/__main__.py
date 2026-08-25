"""CLI entry point: python -m history_graph"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .client import OpenAlexClient
from .ingest import run_ingest

DEFAULT_SEEDS = Path("data/seed/dois.txt")
DEFAULT_RAW_DIR = Path("data/raw")
DEFAULT_MAILTO = "history-graph-poc@example.org"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="history-graph", description=__doc__)
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--mailto",
        default=os.environ.get("OPENALEX_MAILTO", DEFAULT_MAILTO),
        help="Contact email for the OpenAlex polite pool (or OPENALEX_MAILTO env var)",
    )
    parser.add_argument("--max-pages-per-seed", type=int, default=3)
    parser.add_argument("--hydrate-limit", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    client = OpenAlexClient(mailto=args.mailto)
    try:
        manifest = run_ingest(
            client,
            seeds_path=args.seeds,
            raw_dir=args.raw_dir,
            max_pages_per_seed=args.max_pages_per_seed,
            hydrate_limit=args.hydrate_limit,
        )
    finally:
        client.close()

    print("Ingestion complete:")
    for key, value in manifest.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
