"""Record live HTTP interactions into tests/cassettes for offline replay.

Usage:
    uv run python scripts/record_cassettes.py            # thread stage + sci-hub pdfs
    uv run python scripts/record_cassettes.py --thread   # OpenAlex only

Recording hits the real APIs once (OpenAlex polite pool, sci-hub.ru);
everything downstream then replays deterministically.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from history_graph.download import run_download
from history_graph.testing import openalex_client, scihub_client
from history_graph.thread import run_thread

SEEDS = Path("data/seed/computing_thread.yaml")
SCIHUB_DOIS = ("10.1145/3065386", "10.1126/science.1225829")


def record_thread() -> None:
    client = openalex_client("openalex-thread", record=True)
    try:
        manifest = run_thread(client, SEEDS, Path(tempfile.mkdtemp(prefix="hg-record-")))
    finally:
        client.close()
    print(f"thread cassette: {manifest['papers_resolved']} papers resolved")


def record_scihub(attempts: int = 3, wait_s: int = 45) -> None:
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = [Path(tmp) / "dois.txt"]
            inputs[0].write_text("\n".join(SCIHUB_DOIS) + "\n", encoding="utf-8")
            manifest = run_download(
                scihub_client("scihub", record=True), inputs, Path(tmp) / "pdfs"
            )
        if not manifest["pdfs_failed"]:
            print(f"scihub cassette: {manifest['pdfs_downloaded']} PDFs recorded")
            return
        print(f"attempt {attempt}: {manifest['failures']} (likely captcha wall)")
        if attempt < attempts:
            time.sleep(wait_s)
    sys.exit("recording incomplete: sci-hub kept serving captcha pages; rerun later")


PATENTS_QUERIES = ("US2466157A",)


def record_patents() -> None:
    """Seed the patents cassette with canonical patent-links lookups.

    Walled responses are skipped, not fatal; rerun when Google cools down."""
    from history_graph.testing import patent_client
    from history_graph.uspto import PatentError

    client = patent_client("patents", record=True)
    try:
        for pn in PATENTS_QUERIES:
            try:
                found = client.citing(pn, limit=10)
                print(f"patents cassette: {pn} citing total={found['total']}")
            except PatentError as exc:
                print(f"patents cassette: {pn} skipped ({exc}) — rerun --patents later")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", action="store_true", help="record OpenAlex only")
    parser.add_argument("--scihub", action="store_true", help="record sci-hub only")
    parser.add_argument("--patents", action="store_true", help="record Google Patents queries")
    args = parser.parse_args(argv)
    which = args.thread or args.scihub or args.patents
    if not args.scihub or which and args.thread:
        record_thread()
    if not args.thread or which and args.scihub:
        record_scihub()
    if not which or args.patents:
        record_patents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
