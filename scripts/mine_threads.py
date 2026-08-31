#!/usr/bin/env python3
"""Insight harness CLI: uv run python scripts/mine_threads.py [--record].

Fixtures run offline; cassettes need tests/cassettes/openalex*.json (record
mode hits live OpenAlex). HG_UPDATE_GOLDENS=1 rewrites the golden summary."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from history_graph import mine
from history_graph.testing import openalex_client


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="extend cassettes from live OpenAlex")
    args = ap.parse_args()

    def factory(name: str):
        return openalex_client(name, record=args.record)

    report = mine.run_all(factory)
    for r in report["queries"]:
        flag = "PASS" if r["ok"] else "FAIL"
        print(f"{flag} {r['id']:<24} {json.dumps(r['observed'], sort_keys=True)[:110]}")
    import os
    diffs = mine.check_or_write_goldens(report, write=bool(os.environ.get("HG_UPDATE_GOLDENS")))
    print(f"\n{report['passed']}/{report['total']} queries pass")
    for d in diffs:
        print(f"GOLDEN DIFF {d}")
    return 0 if report["passed"] == report["total"] and not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
